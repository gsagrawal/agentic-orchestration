"""FAKED notification service and session-manager callback for expiry. SQLite, so it is
shared across worker processes. `notify` is keyed: the second call for a key sends nothing."""
import sqlite3

from agent_runtime.db import txn

SCHEMA = """
CREATE TABLE IF NOT EXISTS notifications (key TEXT PRIMARY KEY, question TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS expired (session_id TEXT PRIMARY KEY);
"""


class FakeNotifier:
    def __init__(self, path: str):
        self._path = path
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        conn.close()

    def notify(self, key: str, question: str) -> bool:
        """Returns True if this call sent the notification, False if the key was already sent."""
        with txn(self._path) as c:
            return c.execute("INSERT OR IGNORE INTO notifications VALUES (?, ?)", (key, question)).rowcount == 1

    def expire(self, session_id: str) -> None:
        with txn(self._path) as c:
            c.execute("INSERT OR IGNORE INTO expired VALUES (?)", (session_id,))

    def sent(self) -> list[str]:
        with txn(self._path) as c:
            return [r[0] for r in c.execute("SELECT key FROM notifications ORDER BY key")]

    def expired(self) -> list[str]:
        with txn(self._path) as c:
            return [r[0] for r in c.execute("SELECT session_id FROM expired ORDER BY session_id")]
