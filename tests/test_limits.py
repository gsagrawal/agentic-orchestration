"""Limits: the step cap, the waiting expiry and the active-run limit."""

import pytest

from orchestrator import session_log, state_table
from orchestrator.agent_store import AgentStore
from orchestrator.errors import StaleEpoch
from orchestrator.executor import Executor
from orchestrator.session_manager import ManagerConfig, SessionManager
from orchestrator.tool_service import effect_counts
from tests.helpers import Clock


@pytest.fixture
def clock():
    return Clock()


def make_manager(db_path, agents_path, clock, **config):
    return SessionManager(db_path, AgentStore(agents_path), ManagerConfig(**config), clock=clock)


def run_executor(ex, ticks=30):
    for _ in range(ticks):
        ex.tick()


# --- step cap ----------------------------------------------------------------------------


def test_step_cap_stops_the_poison_script(conn, db_path, agents_path, clock):
    manager = make_manager(db_path, agents_path, clock, step_cap=5)
    ex = Executor(db_path, agents_path, "ex-1", clock=clock)  # registers itself
    sid = manager.start_session("t", "poison")  # loops on a tool call forever
    manager.reconcile_once()
    run_executor(ex, ticks=100)

    status = manager.get_status(sid)
    assert (status["state"], status["stop_reason"], status["executor_id"], status["step_count"]) == (
        "stopped", "step_cap", None, 5)
    assert effect_counts(conn) == {(sid, n): 1 for n in range(1, 6)}  # exactly 5 calls were made
    last = session_log.read(conn, sid)[-1]
    assert (last["kind"], last["payload"]) == ("stopped", {"reason": "step_cap"})


def test_a_script_that_uses_exactly_its_cap_still_finishes(conn, db_path, agents_path, clock):
    manager = make_manager(db_path, agents_path, clock, step_cap=2)  # "two-tools" makes 2 calls
    ex = Executor(db_path, agents_path, "ex-1", clock=clock)
    sid = manager.start_session("t", "two-tools")
    manager.reconcile_once()
    run_executor(ex)
    assert manager.get_status(sid)["state"] == "done"


def test_a_stopped_executor_is_not_resumed_into_another_call(conn, db_path, agents_path, clock):
    """If the executor dies after logging "stopped" but before releasing, the next one
    just releases; it does not ask the model again or make a call."""
    manager = make_manager(db_path, agents_path, clock, step_cap=1)
    sid = manager.start_session("t", "poison")
    manager.reconcile_once()
    epoch = manager.get_status(sid)["epoch"]
    for kind, payload in [
        ("model", {"action": {"do": "tool", "tool": "lookup", "args": {"key": "a"}}}),
        ("tool_result", {"step": 1, "result": {}}),
        ("stopped", {"reason": "step_cap"}),
    ]:
        session_log.append(conn, sid, epoch, kind, payload)
    state_table.assign(conn, sid, "ex-2")
    state_table.heartbeat(conn, "ex-2", clock.now)
    run_executor(Executor(db_path, agents_path, "ex-2", clock=clock))
    assert manager.get_status(sid)["state"] == "stopped"
    assert len(session_log.read(conn, sid)) == 3
    assert effect_counts(conn) == {}


# --- waiting expiry ----------------------------------------------------------------------


def start_waiting_session(db_path, agents_path, clock, manager):
    ex = Executor(db_path, agents_path, "ex-1", clock=clock)
    sid = manager.start_session("t", "ask-human")
    manager.reconcile_once()
    run_executor(ex)
    return sid


def test_waiting_session_expires_on_its_own_timer_without_using_an_executor(conn, db_path, agents_path, clock):
    manager = make_manager(db_path, agents_path, clock, wait_timeout_seconds=100)
    sid = start_waiting_session(db_path, agents_path, clock, manager)
    status = manager.get_status(sid)
    assert (status["state"], status["executor_id"], status["wait_deadline"]) == ("waiting", None, clock.now + 100)

    clock.now += 100  # exactly at the deadline: not yet expired
    manager.reconcile_once()
    assert manager.get_status(sid)["state"] == "waiting"

    clock.now += 0.01
    state_table.heartbeat(conn, "ex-2", clock.now)  # a live executor exists and must stay unused
    manager.reconcile_once()
    status = manager.get_status(sid)
    assert (status["state"], status["executor_id"], status["epoch"]) == ("expired", None, 1)
    assert state_table.assigned_to(conn, "ex-1") == state_table.assigned_to(conn, "ex-2") == []


def test_the_two_timers_are_independent(conn, db_path, agents_path, clock):
    """A waiting session past its RUN deadline is left alone; a running one is never EXPIRED."""
    manager = make_manager(db_path, agents_path, clock, wait_timeout_seconds=1000, run_limit_seconds=10)
    waiting = start_waiting_session(db_path, agents_path, clock, manager)
    clock.now += 500  # far past the run limit, well inside the wait timeout
    state_table.heartbeat(conn, "ex-1", clock.now)
    running = manager.start_session("t", "poison")  # new run deadline: 500 + 10 from now
    manager.reconcile_once()
    assert manager.get_status(waiting)["state"] == "waiting"
    assert manager.get_status(running)["state"] == "running"


# --- active-run limit --------------------------------------------------------------------


def test_run_past_its_limit_is_stopped_and_its_executor_is_fenced(conn, db_path, agents_path, clock):
    manager = make_manager(db_path, agents_path, clock, run_limit_seconds=60)
    ex = Executor(db_path, agents_path, "ex-1", clock=clock)
    sid = manager.start_session("t", "poison")
    manager.reconcile_once()
    ex.tick()
    ex.tick()
    log_before, effects_before = session_log.read(conn, sid), effect_counts(conn)

    clock.now += 60  # exactly at the deadline: still running
    manager.reconcile_once()
    assert manager.get_status(sid)["state"] == "running"

    clock.now += 0.01
    state_table.heartbeat(conn, "ex-1", clock.now)  # the executor is healthy; the run is just too long
    manager.reconcile_once()
    status = manager.get_status(sid)
    assert (status["state"], status["stop_reason"], status["executor_id"], status["epoch"]) == (
        "stopped", "run_limit", None, 2)

    # the executor was mid-run: its next write is rejected, and nothing it does lands
    run = ex.runs[sid]
    with pytest.raises(StaleEpoch):
        run.advance()
    assert (session_log.read(conn, sid), effect_counts(conn)) == (log_before, effects_before)


def test_an_overdue_session_on_a_dead_executor_is_stopped_not_moved(conn, db_path, agents_path, clock):
    manager = make_manager(db_path, agents_path, clock, run_limit_seconds=60)
    state_table.heartbeat(conn, "ex-1", clock.now)
    sid = manager.start_session("t", "poison")
    manager.reconcile_once()
    clock.now += 1000  # past the run limit AND past the lease
    state_table.heartbeat(conn, "ex-2", clock.now)  # a live executor that could have taken it
    manager.reconcile_once()
    status = manager.get_status(sid)
    assert (status["state"], status["stop_reason"]) == ("stopped", "run_limit")
    assert state_table.assigned_to(conn, "ex-2") == []
