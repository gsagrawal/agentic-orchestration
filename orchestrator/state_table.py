"""The session state table (owned by the session manager): every query and update on it.

Who writes what: the manager admits sessions, assigns them, and ends overdue ones; an executor
releases a session it holds (done, waiting or stopped), fenced by its epoch.
"""

import sqlite3

from .db import write_txn
from .errors import SessionClosed, StaleEpoch, UnknownSession


def create_session(
    conn: sqlite3.Connection, session_id: str, tenant: str, step_cap: int,
    agent_id: str | None = None, agent_version: int | None = None, run_deadline: float | None = None,
    wait_timeout: float = 3600.0,  # the manager always passes its own; this is for callers that do not care
) -> None:
    conn.execute(
        "INSERT INTO sessions (id, tenant, step_cap, agent_id, agent_version, run_deadline, wait_timeout) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (session_id, tenant, step_cap, agent_id, agent_version, run_deadline, wait_timeout),
    )


def get_session(conn: sqlite3.Connection, session_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if row is None:
        raise UnknownSession(session_id)
    return row


def assign(conn: sqlite3.Connection, session_id: str, executor_id: str) -> int:
    """Give the session to an executor and return the new epoch, unconditionally.

    The bump is what fences the previous holder: its epoch no longer matches.
    A session in a terminal state is never reassigned. The reconciler uses
    assign_if_needed() instead; this plain form is for setting up a situation by hand.
    """
    with write_txn(conn):
        row = conn.execute(
            "UPDATE sessions SET epoch = epoch + 1, executor_id = ?, state = 'running' "
            "WHERE id = ? AND state IN ('created', 'running', 'waiting') RETURNING epoch",
            (executor_id, session_id),
        ).fetchone()
        if row is None:
            get_session(conn, session_id)  # raises UnknownSession if it does not exist
            raise SessionClosed(session_id)
    return row["epoch"]


def assigned_to(conn: sqlite3.Connection, executor_id: str) -> list[tuple[str, int]]:
    """(session id, epoch) of every running session assigned to this executor.

    This is how dispatch works: assign() writes executor_id, the executor polls here.
    """
    rows = conn.execute(
        "SELECT id, epoch FROM sessions WHERE executor_id = ? AND state = 'running' ORDER BY id",
        (executor_id,),
    ).fetchall()
    return [(r["id"], r["epoch"]) for r in rows]


def release(conn: sqlite3.Connection, session_id: str, epoch: int, new_state: str,
            wait_deadline: float | None = None, stop_reason: str | None = None) -> None:
    """The executor gives the session back: 'done', 'stopped' or 'waiting'.
    A waiting session also gets its expiry time, a stopped one its reason. Fenced by the epoch."""
    if new_state not in ("done", "stopped", "waiting"):
        raise ValueError(new_state)
    with write_txn(conn):
        row = conn.execute(
            "UPDATE sessions SET state = ?, executor_id = NULL, wait_deadline = ?, stop_reason = ? "
            "WHERE id = ? AND epoch = ? AND state = 'running' RETURNING id",
            (new_state, wait_deadline, stop_reason, session_id, epoch),
        ).fetchone()
        if row is None:
            get_session(conn, session_id)  # raises UnknownSession if it does not exist
            raise StaleEpoch(f"{session_id}: epoch {epoch} is not current")


def expire_waiting(conn: sqlite3.Connection, now: float) -> list[str]:
    """Waiting sessions past their wait deadline become 'expired'. They hold no executor,
    so there is nothing to fence. Returns the ids."""
    rows = conn.execute(
        "UPDATE sessions SET state = 'expired', wait_deadline = NULL "
        "WHERE state = 'waiting' AND wait_deadline < ? RETURNING id", (now,),
    ).fetchall()
    return [r["id"] for r in rows]


def stop_overdue_runs(conn: sqlite3.Connection, now: float) -> list[str]:
    """Running sessions past their run deadline become 'stopped'. The epoch is bumped so the
    executor still working on one is fenced: its next write or tool call is rejected."""
    rows = conn.execute(
        "UPDATE sessions SET state = 'stopped', stop_reason = 'run_limit', executor_id = NULL, epoch = epoch + 1 "
        "WHERE state = 'running' AND run_deadline < ? RETURNING id", (now,),
    ).fetchall()
    return [r["id"] for r in rows]


LIVE_STATES = ("created", "running", "waiting")
TERMINAL_STATES = ("done", "cancelled", "expired", "stopped")  # never change again


def count_live(conn: sqlite3.Connection, tenant: str) -> int:
    marks = ",".join("?" * len(LIVE_STATES))
    return conn.execute(
        f"SELECT COUNT(*) FROM sessions WHERE tenant = ? AND state IN ({marks})", (tenant, *LIVE_STATES)
    ).fetchone()[0]


def is_over_budget(conn: sqlite3.Connection, tenant: str) -> bool:
    row = conn.execute("SELECT over_budget FROM tenant_budget WHERE tenant = ?", (tenant,)).fetchone()
    return bool(row and row["over_budget"])


def heartbeat(conn: sqlite3.Connection, executor_id: str, now: float) -> None:
    """Record that the executor is alive. The first call registers it."""
    conn.execute(
        "INSERT INTO executors (id, last_heartbeat) VALUES (?, ?) "
        "ON CONFLICT(id) DO UPDATE SET last_heartbeat = excluded.last_heartbeat",
        (executor_id, now),
    )


def least_loaded_executor(conn: sqlite3.Connection, alive_since: float) -> str | None:
    """The live executor (heartbeat at or after `alive_since`) with the fewest running
    sessions, ties broken by id. None if no executor is alive."""
    row = conn.execute(
        "SELECT e.id FROM executors e "
        "LEFT JOIN sessions s ON s.executor_id = e.id AND s.state = 'running' "
        "WHERE e.last_heartbeat >= ? "
        "GROUP BY e.id ORDER BY COUNT(s.id), e.id LIMIT 1",
        (alive_since,),
    ).fetchone()
    return row["id"] if row else None


# A session needs an executor if it was never assigned, or if the executor it runs on
# has not been heard from since `alive_since` (or is unknown). One definition, used to
# list candidates and again to guard the update, so the two cannot drift apart.
_NEEDS_EXECUTOR = """(state = 'created' OR (state = 'running' AND executor_id NOT IN
    (SELECT id FROM executors WHERE last_heartbeat >= :alive_since)))"""


def sessions_needing_executor(conn: sqlite3.Connection, alive_since: float) -> list[str]:
    rows = conn.execute(
        f"SELECT id FROM sessions WHERE {_NEEDS_EXECUTOR} ORDER BY rowid", {"alive_since": alive_since}
    ).fetchall()
    return [r["id"] for r in rows]


def assign_if_needed(conn: sqlite3.Connection, session_id: str, executor_id: str, alive_since: float) -> int | None:
    """Like assign(), but only if the session still needs an executor. Returns the new
    epoch, or None if it no longer does.

    The guard matters: between listing candidates and assigning, a live executor may
    have finished the session or put it to wait, and moving it then would be wrong.
    """
    row = conn.execute(
        f"UPDATE sessions SET epoch = epoch + 1, executor_id = :executor_id, state = 'running' "
        f"WHERE id = :id AND {_NEEDS_EXECUTOR} RETURNING epoch",
        {"executor_id": executor_id, "id": session_id, "alive_since": alive_since},
    ).fetchone()
    return row["epoch"] if row else None
