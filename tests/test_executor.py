"""Executor: replay from the log, scripted model, tool calls with (session, step, epoch)."""

import time

import pytest

from orchestrator import session_log, state_table
from orchestrator.errors import StaleEpoch
from orchestrator.executor import Executor, start_worker
from orchestrator.scripted_model import ScriptedModel
from orchestrator.tool_service import effect_counts


def new_session(conn, agent, sid="s1"):
    state_table.create_session(conn, sid, "t1", step_cap=50, agent_id=agent, agent_version=1)
    return sid


def run_to_end(ex, limit=50):
    for _ in range(limit):
        if ex.tick() == 0:
            return
    raise AssertionError("session did not finish")


def log_shape(conn, sid):
    return [(e["kind"], e["payload"].get("action", {}).get("do") or e["payload"].get("step")) for e in session_log.read(conn, sid)]


def test_runs_script_to_done_and_logs_every_step(conn, db_path, agents_path):
    sid = new_session(conn, "two-tools")
    state_table.assign(conn, sid, "ex-1")
    run_to_end(Executor(db_path, agents_path, "ex-1"))

    assert log_shape(conn, sid) == [
        ("model", "tool"), ("tool_result", 1), ("model", "tool"), ("tool_result", 2), ("model", "finish"),
    ]
    row = state_table.get_session(conn, sid)
    assert (row["state"], row["executor_id"]) == ("done", None)
    assert effect_counts(conn) == {(sid, 1): 1, (sid, 2): 1}  # step numbers are 1-based and sequential


def test_wait_for_human_releases_the_executor(conn, db_path, agents_path):
    sid = new_session(conn, "ask-human")
    state_table.assign(conn, sid, "ex-1")
    ex = Executor(db_path, agents_path, "ex-1")
    run_to_end(ex)
    row = state_table.get_session(conn, sid)
    assert (row["state"], row["executor_id"]) == ("waiting", None)
    assert ex.runs == {}


def test_a_fresh_executor_resumes_from_the_log_without_repeating_finished_steps(conn, db_path, agents_path):
    sid = new_session(conn, "two-tools")
    state_table.assign(conn, sid, "ex-1")
    first = Executor(db_path, agents_path, "ex-1")
    first.tick()  # step 1 done: model entry + tool result in the log
    assert effect_counts(conn) == {(sid, 1): 1}

    state_table.assign(conn, sid, "ex-2")  # ex-1 "dies"; the session moves, epoch 2
    run_to_end(Executor(db_path, agents_path, "ex-2"))

    assert state_table.get_session(conn, sid)["state"] == "done"
    assert effect_counts(conn) == {(sid, 1): 1, (sid, 2): 1}
    epochs = [e["epoch"] for e in session_log.read(conn, sid)]
    assert epochs == [1, 1, 2, 2, 2]  # step 1 written by epoch 1, the rest by epoch 2


def test_model_input_is_rebuilt_from_the_log(conn, db_path, agents_path):
    seen = []

    class Spy(ScriptedModel):
        def decide(self, agent, messages):
            seen.append([(m["role"], m.get("step")) for m in messages])
            return super().decide(agent, messages)

    sid = new_session(conn, "two-tools")
    state_table.assign(conn, sid, "ex-1")
    Executor(db_path, agents_path, "ex-1", model=Spy()).tick()
    state_table.assign(conn, sid, "ex-2")
    run_to_end(Executor(db_path, agents_path, "ex-2", model=Spy()))
    # the resumed executor's first call already sees step 1's call and result
    assert seen[1] == [("assistant", None), ("tool", 1)]
    assert seen[-1] == [("assistant", None), ("tool", 1), ("assistant", None), ("tool", 2)]


def test_zombie_executor_is_rejected_on_its_next_write(conn, db_path, agents_path):
    """ex-1 is paused (zombie) while the session moves to ex-2. When it wakes and takes
    its next step with the old epoch, its write is rejected and nothing changes."""
    sid = new_session(conn, "two-tools")
    e1 = state_table.assign(conn, sid, "ex-1")
    zombie = Executor(db_path, agents_path, "ex-1")
    zombie.tick()  # one step at epoch 1
    run = zombie.runs[sid]
    log_before = session_log.read(conn, sid)

    state_table.assign(conn, sid, "ex-2")
    with pytest.raises(StaleEpoch):
        run.advance()  # the zombie wakes up holding its old run
    assert session_log.read(conn, sid) == log_before
    assert effect_counts(conn) == {(sid, 1): 1}  # the zombie caused no new effect
    assert run.epoch == e1


def test_tick_drops_a_session_that_moved_away_and_does_not_crash(conn, db_path, agents_path):
    sid = new_session(conn, "two-tools")
    state_table.assign(conn, sid, "ex-1")
    ex = Executor(db_path, agents_path, "ex-1")
    ex.tick()
    state_table.assign(conn, sid, "ex-2")
    assert ex.tick() == 0
    assert ex.runs == {}


def test_pending_tool_call_on_resume_fails_closed_until_step_6(conn, db_path, agents_path):
    """PLACEHOLDER: step 6 replaces this test with the real recovery rules."""
    sid = new_session(conn, "two-tools")
    e = state_table.assign(conn, sid, "ex-1")
    session_log.append(conn, sid, e, "model", {"action": {"do": "tool", "tool": "send_email", "args": {"to": "x"}}})
    state_table.assign(conn, sid, "ex-2")
    with pytest.raises(NotImplementedError):
        Executor(db_path, agents_path, "ex-2").tick()
    assert effect_counts(conn) == {}


def test_session_is_pinned_to_its_agent_version(conn, db_path, agents_path):
    sid = new_session(conn, "two-tools")
    row = state_table.get_session(conn, sid)
    assert (row["agent_id"], row["agent_version"]) == ("two-tools", 1)


def test_worker_process_runs_assigned_sessions_to_completion(conn, db_path, agents_path):
    ids = [new_session(conn, "two-tools", f"s{i}") for i in range(3)]
    for sid in ids:
        state_table.assign(conn, sid, "ex-1")
    proc = start_worker(db_path, agents_path, "ex-1")
    try:
        deadline = time.time() + 20
        while time.time() < deadline and any(state_table.get_session(conn, s)["state"] != "done" for s in ids):
            time.sleep(0.05)
        assert [state_table.get_session(conn, s)["state"] for s in ids] == ["done"] * 3
        assert effect_counts(conn) == {(s, n): 1 for s in ids for n in (1, 2)}
    finally:
        proc.kill()
        proc.join()
