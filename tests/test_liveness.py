"""Liveness: heartbeats, the lease, and the reconciler moving sessions off dead executors."""

import time

import pytest

from orchestrator import session_log, state_table
from orchestrator.agent_store import AgentStore
from orchestrator.executor import Executor, start_worker
from orchestrator.scripted_model import ScriptedModel
from orchestrator.session_manager import ManagerConfig, SessionManager
from orchestrator.tool_service import effect_counts
from tests.helpers import Clock, wait_until

LEASE = 3.0


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def manager(db_path, agents_path, clock):
    return SessionManager(db_path, AgentStore(agents_path), ManagerConfig(lease_seconds=LEASE), clock=clock)


def last_heartbeat(conn, executor_id):
    return conn.execute("SELECT last_heartbeat FROM executors WHERE id = ?", (executor_id,)).fetchone()[0]


def test_executor_registers_and_then_heartbeats_no_more_often_than_its_interval(conn, db_path, agents_path, clock):
    ex = Executor(db_path, agents_path, "ex-1", clock=clock, heartbeat_interval=0.5)
    assert last_heartbeat(conn, "ex-1") == 1000.0
    clock.now = 1000.2
    ex.tick()
    assert last_heartbeat(conn, "ex-1") == 1000.0  # too soon
    clock.now = 1000.6
    ex.tick()
    assert last_heartbeat(conn, "ex-1") == 1000.6


def test_an_executor_with_many_sessions_heartbeats_between_them(conn, db_path, agents_path, clock):
    """Each iteration takes 1 s of (fake) time. The heartbeat must not wait for the whole
    sweep over its sessions, or a busy but healthy executor would look dead."""
    class SlowModel(ScriptedModel):
        def decide(self, agent, messages):
            clock.now += 1.0
            return super().decide(agent, messages)

    ex = Executor(db_path, agents_path, "ex-1", clock=clock, heartbeat_interval=0.5, model=SlowModel())
    for i in range(3):
        sid = f"s{i}"
        state_table.create_session(conn, sid, "t", step_cap=10, agent_id="two-tools", agent_version=1)
        state_table.assign(conn, sid, "ex-1")
    start = clock.now
    ex.tick()  # three iterations: the clock ends 3 s later
    assert clock.now == start + 3.0
    assert last_heartbeat(conn, "ex-1") == start + 2.0  # sent before the third session's iteration


@pytest.mark.parametrize("age, still_alive", [(LEASE, True), (LEASE + 0.01, False)])
def test_executor_is_alive_up_to_exactly_one_lease_after_its_last_heartbeat(conn, manager, clock, age, still_alive):
    state_table.heartbeat(conn, "ex-1", clock.now)
    sid = manager.start_session("t", "two-tools")
    manager.reconcile_once()
    first_epoch = manager.get_status(sid)["epoch"]
    state_table.heartbeat(conn, "ex-2", clock.now + age)  # a live place to move to
    clock.now += age
    manager.reconcile_once()
    moved = manager.get_status(sid)["epoch"] != first_epoch
    assert moved is not still_alive


def test_a_dead_executor_is_never_given_a_new_session(conn, manager, clock):
    state_table.heartbeat(conn, "ex-a", clock.now)  # "ex-a" sorts first, so it would win a tie
    clock.now += 10
    state_table.heartbeat(conn, "ex-b", clock.now)
    sid = manager.start_session("t", "two-tools")
    manager.reconcile_once()
    assert manager.get_status(sid)["executor_id"] == "ex-b"


def test_a_live_executor_keeps_its_session(conn, manager, clock):
    state_table.heartbeat(conn, "ex-1", clock.now)
    sid = manager.start_session("t", "two-tools")
    manager.reconcile_once()
    clock.now += LEASE - 1
    state_table.heartbeat(conn, "ex-2", clock.now)
    manager.reconcile_once()
    status = manager.get_status(sid)
    assert (status["executor_id"], status["epoch"]) == ("ex-1", 1)


def test_session_on_a_dead_executor_moves_to_a_live_one_with_a_new_epoch(conn, manager, clock):
    state_table.heartbeat(conn, "ex-1", clock.now)
    sid = manager.start_session("t", "two-tools")
    manager.reconcile_once()
    clock.now += LEASE + 1
    state_table.heartbeat(conn, "ex-2", clock.now)
    manager.reconcile_once()
    status = manager.get_status(sid)
    assert (status["state"], status["executor_id"], status["epoch"]) == ("running", "ex-2", 2)


def test_with_no_live_executor_the_session_stays_put_until_one_appears(conn, manager, clock):
    state_table.heartbeat(conn, "ex-1", clock.now)
    sid = manager.start_session("t", "two-tools")
    manager.reconcile_once()
    clock.now += LEASE + 1
    manager.reconcile_once()
    assert manager.get_status(sid)["epoch"] == 1  # nothing to move it to, so no epoch burned
    state_table.heartbeat(conn, "ex-2", clock.now)
    manager.reconcile_once()
    assert manager.get_status(sid)["executor_id"] == "ex-2"


def test_sessions_that_hold_no_executor_are_left_alone(conn, manager, clock):
    state_table.heartbeat(conn, "ex-1", clock.now)
    waiting = manager.start_session("t", "ask-human")
    done = manager.start_session("t", "two-tools")
    manager.reconcile_once()
    conn.execute("UPDATE sessions SET state = 'waiting', executor_id = NULL WHERE id = ?", (waiting,))
    conn.execute("UPDATE sessions SET state = 'done', executor_id = NULL WHERE id = ?", (done,))
    clock.now += LEASE + 1
    state_table.heartbeat(conn, "ex-2", clock.now)
    manager.reconcile_once()
    assert [manager.get_status(s)["epoch"] for s in (waiting, done)] == [1, 1]


def test_assign_if_needed_refuses_a_session_that_no_longer_needs_an_executor(conn, manager, clock):
    state_table.heartbeat(conn, "ex-1", clock.now)
    sid = manager.start_session("t", "two-tools")
    manager.reconcile_once()
    alive_since = clock.now - LEASE
    assert state_table.assign_if_needed(conn, sid, "ex-2", alive_since) is None  # ex-1 is alive
    conn.execute("UPDATE sessions SET state = 'waiting', executor_id = NULL WHERE id = ?", (sid,))
    assert state_table.assign_if_needed(conn, sid, "ex-2", alive_since) is None  # it was just released
    assert manager.get_status(sid)["epoch"] == 1


def test_session_resumes_on_another_executor_after_its_executor_goes_silent(conn, db_path, agents_path, manager, clock):
    ex1 = Executor(db_path, agents_path, "ex-1", clock=clock)
    sid = manager.start_session("t", "two-tools")
    manager.reconcile_once()
    ex1.tick()  # step 1 done, then ex-1 goes silent: no more ticks, no more heartbeats

    clock.now += LEASE + 1
    ex2 = Executor(db_path, agents_path, "ex-2", clock=clock)
    manager.reconcile_once()
    for _ in range(10):
        ex2.tick()

    assert manager.get_status(sid)["state"] == "done"
    assert effect_counts(conn) == {(sid, 1): 1, (sid, 2): 1}
    assert [e["epoch"] for e in session_log.read(conn, sid)] == [1, 1, 2, 2, 2]

    # ex-1 wakes up: it finds nothing assigned, writes nothing, and the log is unchanged.
    log_before = session_log.read(conn, sid)
    assert ex1.tick() == 0
    assert session_log.read(conn, sid) == log_before


def test_killing_a_real_executor_process_makes_the_manager_see_it_as_dead(conn, db_path, agents_path):
    """Real clock and real SIGKILL. Waiting longer than the lease while it keeps being
    seen alive proves the heartbeat repeats; after the kill it must age out."""
    lease = 1.5

    def alive():
        return state_table.least_loaded_executor(conn, time.time() - lease) == "ex-1"

    proc = start_worker(db_path, agents_path, "ex-1")
    try:
        wait_until(alive, "the worker to register")
        time.sleep(lease + 0.5)
        assert alive(), "worker stopped heartbeating"
        proc.kill()
        proc.join()
        wait_until(lambda: not alive(), "the dead worker to age out")
    finally:
        proc.kill()
        proc.join()
