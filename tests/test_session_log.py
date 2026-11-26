"""Fencing at the session log: a stale epoch can never append."""

import sqlite3
import threading
import time

import pytest

from orchestrator import db, session_log, state_table
from orchestrator.errors import StaleEpoch, UnknownSession


@pytest.fixture
def session(conn):
    state_table.create_session(conn, "s1", tenant="t1", step_cap=10)
    return "s1"


def test_append_with_current_epoch_gets_increasing_seq(conn, session):
    epoch = state_table.assign(conn, session, "ex-A")
    assert session_log.append(conn, session, epoch, "model", {"n": 1}) == 1
    assert session_log.append(conn, session, epoch, "model", {"n": 2}) == 2
    entries = session_log.read(conn, session)
    assert [(e["seq"], e["epoch"], e["payload"]) for e in entries] == [(1, epoch, {"n": 1}), (2, epoch, {"n": 2})]


def test_append_before_any_assignment_is_rejected(conn, session):
    # epoch is 0 until the first assignment; nobody holds the session yet
    with pytest.raises(StaleEpoch):
        session_log.append(conn, session, 1, "model", {})


def test_stale_executor_cannot_append_after_reassignment(conn, session):
    old = state_table.assign(conn, session, "ex-A")
    session_log.append(conn, session, old, "model", {"by": "A"})
    new = state_table.assign(conn, session, "ex-B")
    assert new == old + 1

    with pytest.raises(StaleEpoch):
        session_log.append(conn, session, old, "model", {"by": "zombie A"})
    # the new holder can still write, and the zombie's entry is nowhere in the log
    session_log.append(conn, session, new, "model", {"by": "B"})
    assert [e["payload"]["by"] for e in session_log.read(conn, session)] == ["A", "B"]


def test_zombie_is_rejected_on_every_attempt_not_just_the_first(conn, session):
    old = state_table.assign(conn, session, "ex-A")
    state_table.assign(conn, session, "ex-B")
    for _ in range(3):
        with pytest.raises(StaleEpoch):
            session_log.append(conn, session, old, "model", {})
    assert session_log.read(conn, session) == []


def test_epoch_from_the_future_is_also_rejected(conn, session):
    epoch = state_table.assign(conn, session, "ex-A")
    with pytest.raises(StaleEpoch):
        session_log.append(conn, session, epoch + 1, "model", {})


def test_rejected_append_does_not_consume_a_sequence_number(conn, session):
    old = state_table.assign(conn, session, "ex-A")
    new = state_table.assign(conn, session, "ex-B")
    with pytest.raises(StaleEpoch):
        session_log.append(conn, session, old, "model", {})
    assert session_log.append(conn, session, new, "model", {}) == 1


def test_unknown_session_is_not_reported_as_stale(conn):
    with pytest.raises(UnknownSession):
        session_log.append(conn, "nope", 1, "model", {})


def test_sessions_are_fenced_independently(conn):
    state_table.create_session(conn, "a", "t", 10)
    state_table.create_session(conn, "b", "t", 10)
    ea = state_table.assign(conn, "a", "ex-1")
    state_table.assign(conn, "b", "ex-1")
    state_table.assign(conn, "b", "ex-2")  # bumping b must not fence a
    session_log.append(conn, "a", ea, "model", {})


def test_log_is_append_only(conn, session):
    epoch = state_table.assign(conn, session, "ex-A")
    session_log.append(conn, session, epoch, "model", {})
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("UPDATE session_log SET payload = '{}' WHERE session_id = ?", (session,))
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("DELETE FROM session_log WHERE session_id = ?", (session,))


def test_concurrent_appends_racing_a_reassignment_never_write_out_of_epoch_order(db_path):
    """Four writers append with epoch 1 while the epoch is bumped to 2 mid-flight.

    Once the bump commits, every epoch-1 append must be rejected, so in the
    log all epoch-1 entries come before any epoch-2 entry. Each thread has
    its own connection, as separate executor processes would.
    """
    setup = db.connect(db_path)
    state_table.create_session(setup, "s1", "t", 10_000)
    state_table.assign(setup, "s1", "ex-A")  # epoch 1

    rejected = []
    errors = []

    def zombie_writer():
        c = db.connect(db_path)
        try:
            give_up = time.time() + 10  # only reached if fencing is broken
            while time.time() < give_up:  # normally ends by StaleEpoch after the bump
                try:
                    session_log.append(c, "s1", 1, "model", {})
                except StaleEpoch:
                    rejected.append(1)
                    return
        except Exception as e:  # anything else is a real failure
            errors.append(e)
        finally:
            c.close()

    threads = [threading.Thread(target=zombie_writer) for _ in range(4)]
    for t in threads:
        t.start()
    while len(session_log.read(setup, "s1")) < 50:  # let them get going
        pass
    new = state_table.assign(setup, "s1", "ex-B")  # the reassignment
    for _ in range(20):
        session_log.append(setup, "s1", new, "model", {"by": "B"})
    for t in threads:
        t.join()

    assert not errors
    assert len(rejected) == 4  # every writer was fenced
    epochs = [e["epoch"] for e in session_log.read(setup, "s1")]
    assert epochs == sorted(epochs)
    assert epochs.count(2) == 20
