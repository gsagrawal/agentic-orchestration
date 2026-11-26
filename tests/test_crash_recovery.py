"""A tool call is in flight when its executor dies. What happens on the next executor?

Rules (DESIGN decision 3), applied when the log shows a call with no result:
    result stored in the tool service -> use it
    none, tool idempotent             -> run it again
    none, tool not idempotent         -> tell the model "outcome unknown"; never retry

Part 1 simulates the death with an exception at a fault point (fast, checks the rules and
what the model is shown). Part 2 uses real OS processes and a real SIGKILL.
"""

import signal
import time

import pytest

from orchestrator import session_log, state_table
from orchestrator.agent_store import AgentStore
from orchestrator.errors import StaleEpoch
from orchestrator.executor import OUTCOME_UNKNOWN, Executor, SessionRun, start_worker
from orchestrator.scripted_model import ScriptedModel
from orchestrator.session_manager import ManagerConfig, SessionManager
from orchestrator.tool_service import effect_counts
from tests.helpers import wait_until


class Died(Exception):
    pass


def die_at(target):
    def hook(point):
        if point == target:
            raise Died
    return hook


def kinds(conn, sid):
    return [e["kind"] for e in session_log.read(conn, sid)]


# --- Part 1: the rules, with a simulated death -----------------------------------------


def crash_then_resume(conn, db_path, agents_path, agent, die_point, model=None):
    """ex-1 dies at `die_point`; the session moves to ex-2, which runs it to the end."""
    sid = "s1"
    state_table.create_session(conn, sid, "t", step_cap=10, agent_id=agent, agent_version=1)
    state_table.assign(conn, sid, "ex-1")
    with pytest.raises(Died):
        Executor(db_path, agents_path, "ex-1", fault_hook=die_at(die_point)).tick()
    state_table.assign(conn, sid, "ex-2")
    ex2 = Executor(db_path, agents_path, "ex-2", model=model)
    for _ in range(10):
        ex2.tick()
    assert state_table.get_session(conn, sid)["state"] == "done"
    return sid


def test_stored_result_is_used_not_rerun(conn, db_path, agents_path):
    sid = crash_then_resume(conn, db_path, agents_path, "send-email", "after_tool_call")
    assert kinds(conn, sid) == ["model", "tool_result", "model"]
    assert effect_counts(conn) == {(sid, 1): 1}


def test_idempotent_call_with_no_result_is_retried_once(conn, db_path, agents_path):
    sid = crash_then_resume(conn, db_path, agents_path, "set-value", "before_effect")
    assert kinds(conn, sid) == ["model", "tool_result", "model"]
    assert effect_counts(conn) == {(sid, 1): 1}


def test_non_idempotent_call_that_never_ran_is_reported_unknown_not_retried(conn, db_path, agents_path):
    sid = crash_then_resume(conn, db_path, agents_path, "send-email", "before_effect")
    assert kinds(conn, sid) == ["model", "tool_unknown", "model"]
    assert effect_counts(conn) == {}  # it really did not run, and we did not run it either


def test_non_idempotent_call_that_ran_without_a_stored_result_is_not_run_a_second_time(conn, db_path, agents_path):
    sid = crash_then_resume(conn, db_path, agents_path, "send-email", "after_effect")
    assert kinds(conn, sid) == ["model", "tool_unknown", "model"]
    assert effect_counts(conn) == {(sid, 1): 1}  # one email, and the model was not told it was sent


def test_a_zombie_cannot_recover_a_call_either(conn, db_path, agents_path):
    """ex-2 has picked the session up mid-call but is replaced by ex-3 before it acts.
    Its recovery (lookup, retry) is rejected: no effect, no log entry."""
    sid = "s1"
    state_table.create_session(conn, sid, "t", step_cap=10, agent_id="set-value", agent_version=1)
    state_table.assign(conn, sid, "ex-1")
    with pytest.raises(Died):
        Executor(db_path, agents_path, "ex-1", fault_hook=die_at("before_effect")).tick()

    epoch = state_table.assign(conn, sid, "ex-2")
    ex2 = Executor(db_path, agents_path, "ex-2")
    run = SessionRun(ex2.conn, ex2.tools, ex2.model, ex2.agents, sid, epoch, ex2.fault_hook)  # replayed, not yet advanced
    state_table.assign(conn, sid, "ex-3")

    log_before = session_log.read(conn, sid)
    with pytest.raises(StaleEpoch):
        run.advance()
    assert session_log.read(conn, sid) == log_before
    assert effect_counts(conn) == {}


def test_the_model_is_shown_outcome_unknown(conn, db_path, agents_path):
    seen = []

    class Spy(ScriptedModel):
        def decide(self, agent, messages):
            seen.append(list(messages))  # a snapshot: the executor keeps appending to its list
            return super().decide(agent, messages)

    crash_then_resume(conn, db_path, agents_path, "send-email", "after_effect", model=Spy())
    assert seen[-1][-1] == {"role": "tool", "step": 1, "content": OUTCOME_UNKNOWN}


# --- Part 2: real processes, real kill -9 ----------------------------------------------

LEASE = 0.6       # short so a scenario takes about a second; ASSUMPTION for these tests only
HEARTBEAT = 0.1


def run_until_done(manager, sid):
    wait_until(lambda: (manager.reconcile_once(), manager.get_status(sid)["state"] == "done")[1], "session to finish")


def crash_scenario(conn, db_path, agents_path, agent, kill):
    """Start ex-1, let `kill` (a fault point name, or None for an outside SIGKILL) stop it,
    then start ex-2 and let the manager's reconciler move the session. Returns the session id."""
    manager = SessionManager(db_path, AgentStore(agents_path), ManagerConfig(lease_seconds=LEASE))
    procs = []
    try:
        first = start_worker(db_path, agents_path, "ex-1", heartbeat_interval=HEARTBEAT,
                             kill_at=kill, tool_delay=0.0 if kill else 30.0)
        procs.append(first)
        wait_until(lambda: conn.execute("SELECT 1 FROM executors").fetchone(), "ex-1 to register")
        sid = manager.start_session("t", agent)
        manager.reconcile_once()
        if kill:
            first.join(timeout=20)
        else:  # outside kill: wait until the call is in flight (decision logged), then SIGKILL
            wait_until(lambda: "model" in kinds(conn, sid), "the call to start")
            first.kill()
            first.join()
        assert first.exitcode == -signal.SIGKILL
        procs.append(start_worker(db_path, agents_path, "ex-2", heartbeat_interval=HEARTBEAT))
        run_until_done(manager, sid)
        return sid
    finally:
        for p in procs:
            p.kill()
            p.join()


# (agent, where ex-1 is killed) -> (effects of the one call, what the log records for it)
KILL_POINTS = [
    ("set-value", "before_tool_call", 1, "tool_result"),    # idempotent: retried
    ("set-value", "before_effect", 1, "tool_result"),
    ("set-value", "after_tool_call", 1, "tool_result"),     # result stored: used
    ("set-value", "after_result_logged", 1, "tool_result"),  # nothing in flight: just continues
    ("send-email", "before_tool_call", 0, "tool_unknown"),  # not idempotent, no result: unknown
    ("send-email", "before_effect", 0, "tool_unknown"),
    ("send-email", "after_effect", 1, "tool_unknown"),      # it ran, nobody recorded it: still not rerun
    ("send-email", "after_tool_call", 1, "tool_result"),
    ("send-email", "after_result_logged", 1, "tool_result"),
]


@pytest.mark.parametrize("agent, kill, effects, recorded", KILL_POINTS)
def test_kill_9_at_each_point_of_a_tool_call(conn, db_path, agents_path, agent, kill, effects, recorded):
    sid = crash_scenario(conn, db_path, agents_path, agent, kill)
    assert effect_counts(conn).get((sid, 1), 0) == effects
    assert kinds(conn, sid).count(recorded) == 1
    assert kinds(conn, sid).count("tool_result") + kinds(conn, sid).count("tool_unknown") == 1
    assert [e["epoch"] for e in session_log.read(conn, sid)][-1] == 2  # ex-2 finished it


@pytest.mark.parametrize("agent, effects, recorded", [("set-value", 1, "tool_result"), ("send-email", 0, "tool_unknown")])
def test_kill_9_from_outside_in_the_middle_of_a_slow_tool_call(conn, db_path, agents_path, agent, effects, recorded):
    """The tool is held in its 30 s delay (ex-2 runs with none); the test kills ex-1 once the
    decision is logged. The kill lands somewhere before the effect; the exact point is not
    pinned here (the fault-point table above does that). This checks an outside kill works."""
    sid = crash_scenario(conn, db_path, agents_path, agent, kill=None)
    assert effect_counts(conn).get((sid, 1), 0) == effects
    assert kinds(conn, sid).count(recorded) == 1
