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
import os
import sqlite3
import time
from dataclasses import dataclass

from agent_scheduler_temporal.db import txn
from agent_scheduler_temporal.fault import maybe_fault
from agent_scheduler_temporal.tools import FakeTools, ToolSpec

SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    key             TEXT PRIMARY KEY,
    highest_attempt INTEGER NOT NULL,
    started         INTEGER NOT NULL DEFAULT 0,   -- the "started" marker
    result          TEXT                          -- JSON; NULL until recorded
);
CREATE TABLE IF NOT EXISTS attempts (id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT, attempt INTEGER, at REAL, pid INTEGER);
CREATE TABLE IF NOT EXISTS fenced (id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT, attempt INTEGER);
CREATE TABLE IF NOT EXISTS effects (              -- the ledger; append-only
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    key     TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    tool    TEXT NOT NULL
);
"""

OK = "ok"
OUTCOME_UNKNOWN = "outcome_unknown"


class UnknownTool(Exception):
    """The tool service has no metadata for this tool: a data error, retrying cannot fix it."""


class StaleAttempt(Exception):
    """The call carried an attempt lower than one already seen for this key."""


@dataclass(frozen=True)
class ToolResult:
    status: str  # OK or OUTCOME_UNKNOWN
    value: dict | None = None


class EffectsLedger:
    """Append-only record of real tool effects. Used by the fake tools and by tests."""

    def __init__(self, path: str):
        self.path = path

    def record(self, key: str, attempt: int, tool: str) -> None:
        with txn(self.path) as c:
            c.execute("INSERT INTO effects (key, attempt, tool) VALUES (?, ?, ?)", (key, attempt, tool))

    def record_once(self, key: str, attempt: int, tool: str) -> None:
        """Idempotent backend: a second effect for the same key is a no-op."""
        with txn(self.path) as c:
            if c.execute("SELECT 1 FROM effects WHERE key = ?", (key,)).fetchone() is None:
                c.execute("INSERT INTO effects (key, attempt, tool) VALUES (?, ?, ?)", (key, attempt, tool))

    def count(self, key: str) -> int:
        with txn(self.path) as c:
            return c.execute("SELECT COUNT(*) FROM effects WHERE key = ?", (key,)).fetchone()[0]

    def counts(self) -> dict[str, int]:
        """Effects per key, for every key that has any."""
        with txn(self.path) as c:
            return dict(c.execute("SELECT key, COUNT(*) FROM effects GROUP BY key").fetchall())

    def duplicates(self) -> list[str]:
        """Keys with more than one effect. The chaos invariant is that this is empty."""
        with txn(self.path) as c:
            rows = c.execute("SELECT key FROM effects GROUP BY key HAVING COUNT(*) > 1").fetchall()
        return [r[0] for r in rows]


class ToolService:
    def __init__(self, path: str, tools: FakeTools | None = None, specs: dict[str, ToolSpec] | None = None):
        self.path = path
        self._tools = tools or FakeTools()
        self._specs = specs or FakeTools.SPECS
        self.ledger = EffectsLedger(path)
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        conn.close()

    def execute(self, key: str, attempt: int, tool: str, args: dict) -> ToolResult:
        spec = self._specs.get(tool)
        if spec is None:
            raise UnknownTool(tool)
        maybe_fault("before_marker")

        # Transaction 1: fence, then decide, then write the started marker.
        decision, stored = self._begin(key, attempt, spec)
        if decision == "stale":
            self._reject(key, attempt, "at start")
        if decision == "done":
            return ToolResult(OK, stored)
        if decision == "unknown":
            return ToolResult(OUTCOME_UNKNOWN)

        # Run outside any transaction: it can be slow, and a kill can land here.
        maybe_fault("before_effect")
        value = self._tools.run(self.ledger, spec, key, attempt, args)
        maybe_fault("after_effect")

        # Transaction 2: record the result, unless a newer attempt has taken over.
        with txn(self.path) as c:
            highest = c.execute("SELECT highest_attempt FROM calls WHERE key = ?", (key,)).fetchone()[0]
            stale = attempt < highest
            if not stale:
                c.execute("UPDATE calls SET result = ? WHERE key = ?", (json.dumps(value), key))
        if stale:
            self._reject(key, attempt, "when recording the result")
        maybe_fault("after_result")
        return ToolResult(OK, value)

    def _begin(self, key: str, attempt: int, spec: ToolSpec) -> tuple[str, dict | None]:
        """Returns ('run'|'done'|'unknown'|'stale', stored_result)."""
        with txn(self.path) as c:
            row = c.execute("SELECT highest_attempt, started, result FROM calls WHERE key = ?", (key,)).fetchone()
            if row is None:
                c.execute("INSERT INTO calls (key, highest_attempt, started) VALUES (?, ?, 1)", (key, attempt))
                decision, stored = "run", None
            else:
                highest, started, result = row
                if attempt < highest:
                    return "stale", None
                c.execute("UPDATE calls SET highest_attempt = ? WHERE key = ?", (attempt, key))
                if result is not None:
                    decision, stored = "done", json.loads(result)
                elif started and not spec.idempotent:
                    decision, stored = "unknown", None
                else:
                    decision, stored = "run", None  # marker + idempotent tool: run again
            c.execute("INSERT INTO attempts (key, attempt, at, pid) VALUES (?, ?, ?, ?)", (key, attempt, time.time(), os.getpid()))
        return decision, stored

    def _reject(self, key: str, attempt: int, where: str):
        with txn(self.path) as c:  # separate transaction: the rejection itself must be recorded
            c.execute("INSERT INTO fenced (key, attempt) VALUES (?, ?)", (key, attempt))
        raise StaleAttempt(f"{key}: attempt {attempt} is lower than one already seen ({where})")

    def fenced(self) -> list[tuple[str, int]]:
        """Calls that were rejected as stale. Lets a test show a zombie really hit the fence."""
        with txn(self.path) as c:
            return [(r[0], r[1]) for r in c.execute("SELECT key, attempt FROM fenced ORDER BY id")]

    def attempts(self, key: str) -> list[tuple[int, float]]:
        """(attempt, wall-clock time) for every call that passed the fence; used to time recovery."""
        return [(a, at) for a, at, _ in self.attempts_with_pid(key)]

    def attempts_with_pid(self, key: str) -> list[tuple[int, float, int]]:
        with txn(self.path) as c:
            return [tuple(r) for r in c.execute("SELECT attempt, at, pid FROM attempts WHERE key = ? ORDER BY id", (key,))]

    def state(self, key: str) -> str:
        """'none' (no marker), 'started' (marker, no result) or 'done' (result stored)."""
        with txn(self.path) as c:
            row = c.execute("SELECT started, result FROM calls WHERE key = ?", (key,)).fetchone()
        if row is None:
            return "none"
        return "done" if row[1] is not None else "started"
