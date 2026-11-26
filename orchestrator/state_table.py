"""The session state table (owned by the session manager).

Step 1 only has what the log tests need: create a session, read it, and
assign it (which bumps the epoch). Admission, quotas and the reconciler
come in later steps.
"""

import sqlite3

from .db import write_txn
from .errors import UnknownSession


def create_session(conn: sqlite3.Connection, session_id: str, tenant: str, step_cap: int) -> None:
    conn.execute(
        "INSERT INTO sessions (id, tenant, step_cap) VALUES (?, ?, ?)",
        (session_id, tenant, step_cap),
    )


def get_session(conn: sqlite3.Connection, session_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if row is None:
        raise UnknownSession(session_id)
    return row


def assign(conn: sqlite3.Connection, session_id: str, executor_id: str) -> int:
    """Give the session to an executor and return the new epoch.

    The bump is what fences the previous holder: its epoch no longer matches.
    """
    with write_txn(conn):
        row = conn.execute(
            "UPDATE sessions SET epoch = epoch + 1, executor_id = ?, state = 'running' "
            "WHERE id = ? RETURNING epoch",
            (executor_id, session_id),
        ).fetchone()
    if row is None:
        raise UnknownSession(session_id)
    return row["epoch"]
