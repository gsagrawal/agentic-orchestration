"""Tool service: stands in for the network proxy plus external APIs (FAKED as a whole).

Per idempotency key (session id + step) it keeps:
  * the highest activity attempt seen  -> fencing: a lower attempt is rejected
  * a "started" marker                 -> written *before* the tool runs
  * the stored result                  -> a retry returns it instead of running again
and an effects ledger of everything the tools actually did.

Decision table for execute(key, attempt):
  attempt < highest seen                       -> StaleAttempt (zombie worker)
  result stored                                -> return it, run nothing
  started marker, no result, idempotent tool   -> run again (backend dedupes on key)
  started marker, no result, NOT idempotent    -> "outcome_unknown", run nothing
  no marker                                    -> write marker, run, store result

SQLite (stdlib) is shared by several processes: one connection per call, WAL mode,
and BEGIN IMMEDIATE so the check-and-write steps are atomic across processes.
"""
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass

from agent_runtime.tools import FakeTools, ToolSpec

SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    key             TEXT PRIMARY KEY,
    highest_attempt INTEGER NOT NULL,
    started         INTEGER NOT NULL DEFAULT 0,   -- the "started" marker
    result          TEXT                          -- JSON; NULL until recorded
);
CREATE TABLE IF NOT EXISTS effects (              -- the ledger; append-only
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    key     TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    tool    TEXT NOT NULL
);
"""

OK = "ok"
OUTCOME_UNKNOWN = "outcome_unknown"


class StaleAttempt(Exception):
    """The call carried an attempt lower than one already seen for this key."""


@dataclass(frozen=True)
class ToolResult:
    status: str  # OK or OUTCOME_UNKNOWN
    value: dict | None = None


class EffectsLedger:
    """Append-only record of real tool effects. Used by the fake tools and by tests."""

    def __init__(self, path: str):
        self._path = path

    def record(self, key: str, attempt: int, tool: str) -> None:
        with _txn(self._path) as c:
            c.execute("INSERT INTO effects (key, attempt, tool) VALUES (?, ?, ?)", (key, attempt, tool))

    def record_once(self, key: str, attempt: int, tool: str) -> None:
        """Idempotent backend: a second effect for the same key is a no-op."""
        with _txn(self._path) as c:
            if c.execute("SELECT 1 FROM effects WHERE key = ?", (key,)).fetchone() is None:
                c.execute("INSERT INTO effects (key, attempt, tool) VALUES (?, ?, ?)", (key, attempt, tool))

    def count(self, key: str) -> int:
        with _txn(self._path) as c:
            return c.execute("SELECT COUNT(*) FROM effects WHERE key = ?", (key,)).fetchone()[0]

    def duplicates(self) -> list[str]:
        """Keys with more than one effect. The chaos invariant is that this is empty."""
        with _txn(self._path) as c:
            rows = c.execute("SELECT key FROM effects GROUP BY key HAVING COUNT(*) > 1").fetchall()
        return [r[0] for r in rows]


@contextmanager
def _txn(path: str):
    # isolation_level=None: we issue BEGIN IMMEDIATE ourselves, so the write lock is
    # taken up front and two processes cannot both pass a check and then both write.
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


class ToolService:
    def __init__(self, path: str, tools: FakeTools | None = None, specs: dict[str, ToolSpec] | None = None):
        self._path = path
        self._tools = tools or FakeTools()
        self._specs = specs or FakeTools.SPECS
        self.ledger = EffectsLedger(path)
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        conn.close()

    def execute(self, key: str, attempt: int, tool: str, args: dict) -> ToolResult:
        spec = self._specs[tool]

        # Transaction 1: fence, then decide, then write the started marker.
        with _txn(self._path) as c:
            row = c.execute("SELECT highest_attempt, started, result FROM calls WHERE key = ?", (key,)).fetchone()
            if row is None:
                c.execute("INSERT INTO calls (key, highest_attempt, started) VALUES (?, ?, 1)", (key, attempt))
            else:
                highest, started, result = row
                if attempt < highest:
                    raise StaleAttempt(f"{key}: attempt {attempt} < highest seen {highest}")
                c.execute("UPDATE calls SET highest_attempt = ? WHERE key = ?", (attempt, key))
                if result is not None:
                    return ToolResult(OK, json.loads(result))
                if started and not spec.idempotent:
                    return ToolResult(OUTCOME_UNKNOWN)
                # started marker + idempotent tool: fall through and run again.

        # Run outside any transaction: it can be slow, and a kill can land here.
        value = self._tools.run(self.ledger, spec, key, attempt, args)

        # Transaction 2: record the result, unless a newer attempt has taken over.
        with _txn(self._path) as c:
            highest = c.execute("SELECT highest_attempt FROM calls WHERE key = ?", (key,)).fetchone()[0]
            if attempt < highest:
                raise StaleAttempt(f"{key}: attempt {attempt} < highest seen {highest} (result not recorded)")
            c.execute("UPDATE calls SET result = ? WHERE key = ?", (json.dumps(value), key))
        return ToolResult(OK, value)

    def state(self, key: str) -> str:
        """'none' (no marker), 'started' (marker, no result) or 'done' (result stored)."""
        with _txn(self._path) as c:
            row = c.execute("SELECT started, result FROM calls WHERE key = ?", (key,)).fetchone()
        if row is None:
            return "none"
        return "done" if row[1] is not None else "started"
