"""FAKED: stands in for agent-network-proxy plus the external APIs behind it.

What is real here is the logic the architecture asks of the proxy:
  * every call carries (session, step, epoch); a stale epoch is rejected;
  * the idempotency key is "<session_id>:<step>"; a result is stored per key,
    so a retry returns the stored result instead of running the tool again;
  * every real run is written to the effects ledger so tests can count doubles.

What is faked: the stub is stronger than a real proxy. It re-checks the epoch
in the same transaction that commits the effect. A real proxy cannot do that
for an external API: a request already forwarded when the epoch changes still
takes effect, and only the idempotency key protects against its retry.

Warning for callers: for a NON-idempotent tool, call() runs it again if no
result is stored. Whether to call again after a crash is the executor's decision.
"""

import json
import sqlite3
import time
from typing import Callable

from .db import connect, write_txn
from .errors import KeyConflict, StaleEpoch, UnknownSession
from .fake_tools import Tool


def idempotency_key(session_id: str, step: int) -> str:
    return f"{session_id}:{step}"


class ToolService:
    def __init__(self, db_path: str, tools: dict[str, Tool], fault_hook: Callable[[str], None] | None = None):
        """`fault_hook(point)` is called at "before_effect" and "after_effect" so tests
        can kill the process, or change the epoch, at exactly that moment."""
        self._conn = connect(db_path)
        self._tools = tools
        self._fault = fault_hook or (lambda point: None)

    def is_idempotent(self, tool_name: str) -> bool:
        return self._tools[tool_name].idempotent

    def lookup(self, session_id: str, step: int, epoch: int) -> dict | None:
        """The stored result for this call, or None. Used by an executor resuming after a crash."""
        self._check_epoch(session_id, epoch)
        row = self._conn.execute(
            "SELECT result FROM tool_results WHERE idem_key = ?", (idempotency_key(session_id, step),)
        ).fetchone()
        return json.loads(row["result"]) if row else None

    def call(self, session_id: str, step: int, epoch: int, tool_name: str, args: dict) -> dict:
        tool = self._tools[tool_name]
        key = idempotency_key(session_id, step)
        args_json = json.dumps(args, sort_keys=True)

        self._check_epoch(session_id, epoch)  # fast reject
        stored = self._stored(key, tool_name, args_json)
        if stored is not None:
            return stored

        time.sleep(tool.delay)  # a kill landing here is "killed before the tool ran"
        self._fault("before_effect")

        with write_txn(self._conn):
            # Re-check inside the transaction: a zombie that passed the check above
            # and then slept must still be stopped at the moment of the effect.
            self._check_epoch(session_id, epoch)
            stored = self._stored(key, tool_name, args_json)  # a retry may have finished first
            if stored is not None:
                return stored
            effect_id = self._conn.execute(
                "INSERT INTO effects (session_id, step, tool, epoch, session_epoch) "
                "VALUES (?, ?, ?, ?, (SELECT epoch FROM sessions WHERE id = ?))",
                (session_id, step, tool_name, epoch, session_id),
            ).lastrowid
            result = tool.run(args, effect_id)
            if tool.idempotent:
                self._store(key, session_id, step, tool_name, args_json, result)
                return result

        # Non-idempotent: the effect is committed but the result is not yet recorded. The
        # response takes as long to come back as the request took to go, so spend the delay here too.
        time.sleep(tool.delay)
        self._fault("after_effect")
        # Stored whatever the epoch now is: the effect happened, and recording the
        # truth lets a later lookup find it. A zombie's own log append is still fenced.
        self._store(key, session_id, step, tool_name, args_json, result)
        return result

    def _check_epoch(self, session_id: str, epoch: int) -> None:
        row = self._conn.execute("SELECT epoch FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if row is None:
            raise UnknownSession(session_id)
        if row["epoch"] != epoch:
            raise StaleEpoch(f"{session_id}: epoch {epoch} is not current")

    def _stored(self, key: str, tool_name: str, args_json: str) -> dict | None:
        row = self._conn.execute(
            "SELECT tool, args, result FROM tool_results WHERE idem_key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        if (row["tool"], row["args"]) != (tool_name, args_json):
            raise KeyConflict(f"{key} was used for {row['tool']}{row['args']}, now {tool_name}{args_json}")
        return json.loads(row["result"])

    def _store(self, key, session_id, step, tool_name, args_json, result) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO tool_results (idem_key, session_id, step, tool, args, result) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (key, session_id, step, tool_name, args_json, json.dumps(result)),
        )


def effect_counts(conn: sqlite3.Connection, session_id: str | None = None) -> dict[tuple[str, int], int]:
    """Ledger rows per (session, step). Any count above 1 is a double effect. For tests."""
    sql = "SELECT session_id, step, COUNT(*) AS n FROM effects"
    params: tuple = ()
    if session_id is not None:
        sql, params = sql + " WHERE session_id = ?", (session_id,)
    rows = conn.execute(sql + " GROUP BY session_id, step", params).fetchall()
    return {(r["session_id"], r["step"]): r["n"] for r in rows}
