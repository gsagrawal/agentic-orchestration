"""FAKED sandbox controller. Records every call and says whether a sandbox was newly created.

State is in SQLite so it is shared between worker processes, like the real thing would be.
"""
import sqlite3


class FakeSandboxController:
    def __init__(self, path: str):
        self._path = path
        with self._conn() as c:
            c.executescript(
                "CREATE TABLE IF NOT EXISTS sandboxes (session_id TEXT PRIMARY KEY, live INTEGER NOT NULL);"
                "CREATE TABLE IF NOT EXISTS sandbox_calls ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, op TEXT, created INTEGER);"
            )

    def _conn(self):
        return sqlite3.connect(self._path, timeout=30)

    def ensure(self, session_id: str) -> bool:
        """Returns True if a sandbox was newly created (False: the session already had one)."""
        with self._conn() as c:
            row = c.execute("SELECT live FROM sandboxes WHERE session_id = ?", (session_id,)).fetchone()
            created = row is None or not row[0]
            c.execute("INSERT OR REPLACE INTO sandboxes VALUES (?, 1)", (session_id,))
            c.execute("INSERT INTO sandbox_calls (session_id, op, created) VALUES (?, 'ensure', ?)", (session_id, created))
        return created

    def stop(self, session_id: str) -> None:
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO sandboxes VALUES (?, 0)", (session_id,))
            c.execute("INSERT INTO sandbox_calls (session_id, op, created) VALUES (?, 'stop', 0)", (session_id,))

    def lose(self, session_id: str) -> None:
        """Test hook: the sandbox died on its own. Nobody is told; the next ensure() recreates it."""
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO sandboxes VALUES (?, 0)", (session_id,))

    def calls(self, session_id: str) -> list[tuple[str, bool]]:
        with self._conn() as c:
            rows = c.execute("SELECT op, created FROM sandbox_calls WHERE session_id = ? ORDER BY id", (session_id,))
            return [(op, bool(created)) for op, created in rows]
