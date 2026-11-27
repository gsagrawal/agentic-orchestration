"""SQLite transaction helper shared by the tool service and the session manager."""
import sqlite3
from contextlib import contextmanager


@contextmanager
def txn(path: str):
    """One connection per call; BEGIN IMMEDIATE takes the write lock up front, so two processes
    cannot both pass a check and then both write. isolation_level=None: we manage BEGIN/COMMIT."""
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
