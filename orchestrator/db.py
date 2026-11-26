"""SQLite plumbing: one file holds the state table, the session log and the tool service tables.

Every process (session manager, each executor) opens its own connection.
WAL mode lets readers run while one writer commits.
"""

import sqlite3
from contextlib import contextmanager

# Session states. Terminal states never change again.
#   created   admitted, waiting for an executor
#   running   assigned to an executor
#   waiting   paused until a human responds; holds no executor
#   done / cancelled / expired / stopped   terminal
#     (stopped = killed by the step cap or the active-run limit)
SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id            TEXT PRIMARY KEY,
    tenant        TEXT NOT NULL,
    state         TEXT NOT NULL DEFAULT 'created'
                  CHECK (state IN ('created','running','waiting',
                                   'done','cancelled','expired','stopped')),
    executor_id   TEXT,                       -- NULL when no executor holds the session
    epoch         INTEGER NOT NULL DEFAULT 0, -- bumped on every assignment
    run_deadline  REAL,                       -- active-run limit (unix seconds)
    wait_deadline REAL,                       -- waiting expiry (unix seconds), set while waiting
    step_cap      INTEGER NOT NULL,           -- max tool calls for this session
    step_count    INTEGER NOT NULL DEFAULT 0, -- tool calls made so far
    log_seq       INTEGER NOT NULL DEFAULT 0  -- number of log entries; next entry is log_seq + 1
);

CREATE TABLE IF NOT EXISTS session_log (
    session_id TEXT    NOT NULL REFERENCES sessions(id),
    seq        INTEGER NOT NULL,
    epoch      INTEGER NOT NULL,  -- epoch of the writer, kept so tests can audit it
    kind       TEXT    NOT NULL,
    payload    TEXT    NOT NULL,  -- JSON
    PRIMARY KEY (session_id, seq)
);

-- The log is append-only: reject any edit or delete, even from our own code.
CREATE TRIGGER IF NOT EXISTS session_log_no_update BEFORE UPDATE ON session_log
BEGIN SELECT RAISE(ABORT, 'session_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS session_log_no_delete BEFORE DELETE ON session_log
BEGIN SELECT RAISE(ABORT, 'session_log is append-only'); END;

-- Tool service tables (see tool_service.py).
-- One stored result per idempotency key ("<session_id>:<step>").
CREATE TABLE IF NOT EXISTS tool_results (
    idem_key   TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    step       INTEGER NOT NULL,
    tool       TEXT NOT NULL,
    args       TEXT NOT NULL,  -- canonical JSON, to catch a key reused for a different call
    result     TEXT NOT NULL   -- JSON
);

-- Effects ledger: one row per time a tool REALLY ran. Deliberately has no
-- uniqueness constraint: tests count rows per (session, step) to find double effects.
CREATE TABLE IF NOT EXISTS effects (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT    NOT NULL,
    step       INTEGER NOT NULL,
    tool       TEXT    NOT NULL,
    epoch      INTEGER NOT NULL
);
CREATE TRIGGER IF NOT EXISTS effects_no_update BEFORE UPDATE ON effects
BEGIN SELECT RAISE(ABORT, 'effects is append-only'); END;
CREATE TRIGGER IF NOT EXISTS effects_no_delete BEFORE DELETE ON effects
BEGIN SELECT RAISE(ABORT, 'effects is append-only'); END;
"""


def connect(path: str) -> sqlite3.Connection:
    """Open a connection. Autocommit; use write_txn() for transactions."""
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(path: str) -> None:
    conn = connect(path)
    conn.executescript(SCHEMA)
    conn.close()


@contextmanager
def write_txn(conn: sqlite3.Connection):
    """BEGIN IMMEDIATE takes the write lock up front, so a read-then-write
    inside the block cannot be interleaved with another writer."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
