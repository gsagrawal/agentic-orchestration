"""The session log: append-only, written only by the executor holding the epoch.

The fencing check and the insert happen in ONE transaction. The UPDATE on the
sessions row does two jobs: it only matches if the epoch is current (the
fence), and it hands out the next sequence number. If it matches nothing, no
entry is written.
"""

import json
import sqlite3

from .db import write_txn
from .errors import StaleEpoch, UnknownSession


def append(conn: sqlite3.Connection, session_id: str, epoch: int, kind: str, payload: dict) -> int:
    """Append one entry and return its sequence number (starting at 1).

    Raises StaleEpoch if `epoch` is not the session's current epoch.
    """
    body = json.dumps(payload)  # serialize first, so a bad payload fails before any write
    with write_txn(conn):
        row = conn.execute(
            "UPDATE sessions SET log_seq = log_seq + 1 "
            "WHERE id = ? AND epoch = ? RETURNING log_seq",
            (session_id, epoch),
        ).fetchone()
        if row is None:
            exists = conn.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone()
            raise StaleEpoch(f"{session_id}: epoch {epoch} is not current") if exists else UnknownSession(session_id)
        seq = row["log_seq"]
        conn.execute(
            "INSERT INTO session_log (session_id, seq, epoch, kind, payload) VALUES (?, ?, ?, ?, ?)",
            (session_id, seq, epoch, kind, body),
        )
    return seq


def read(conn: sqlite3.Connection, session_id: str) -> list[dict]:
    """All entries for the session in order. Reads need no epoch: replay is read-only."""
    rows = conn.execute(
        "SELECT seq, epoch, kind, payload FROM session_log WHERE session_id = ? ORDER BY seq",
        (session_id,),
    ).fetchall()
    return [
        {"seq": r["seq"], "epoch": r["epoch"], "kind": r["kind"], "payload": json.loads(r["payload"])}
        for r in rows
    ]
