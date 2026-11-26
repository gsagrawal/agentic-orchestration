"""The invariant checker must itself catch violations; otherwise a green chaos test means nothing."""

import pytest

from orchestrator import invariants, session_log, state_table
from orchestrator.fake_tools import default_tools
from orchestrator.tool_service import ToolService


def finished_session(conn, db_path, sid="s1"):
    """A well-formed finished session: one tool call, then finish."""
    state_table.create_session(conn, sid, "t", step_cap=5, agent_id="a", agent_version=1)
    epoch = state_table.assign(conn, sid, "ex-1")
    call = {"do": "tool", "tool": "lookup", "args": {"key": "a"}}
    session_log.append(conn, sid, epoch, "model", {"action": call}, counts_as_step=True)
    result = ToolService(db_path, default_tools()).call(sid, 1, epoch, "lookup", {"key": "a"})
    session_log.append(conn, sid, epoch, "tool_result", {"step": 1, "result": result})
    session_log.append(conn, sid, epoch, "model", {"action": {"do": "finish"}})
    state_table.release(conn, sid, epoch, "done")
    return epoch


def test_a_healthy_run_has_no_problems(conn, db_path):
    finished_session(conn, db_path)
    assert invariants.check(conn) == []


def test_unfinished_session_is_reported(conn):
    state_table.create_session(conn, "s1", "t", step_cap=5)
    assert invariants.check(conn) == ["s1: not finished, state is created"]


def test_duplicate_effect_is_reported(conn, db_path):
    finished_session(conn, db_path)
    conn.execute("INSERT INTO effects (session_id, step, tool, epoch, session_epoch) VALUES ('s1', 1, 'lookup', 1, 1)")
    assert invariants.check(conn) == ["s1 step 1: tool ran 2 times"]


def test_log_entry_from_an_older_epoch_after_a_newer_one_is_reported(conn, db_path):
    epoch = finished_session(conn, db_path)
    # forge what a zombie's write would look like if the fence ever failed
    conn.execute("INSERT INTO session_log (session_id, seq, epoch, kind, payload) VALUES ('s1', 99, ?, 'model', '{\"action\": {\"do\": \"finish\"}}')",
                 (epoch - 1,))
    problems = invariants.check(conn)
    assert any("older epoch after a newer one" in p for p in problems)


def test_entry_beyond_the_sessions_epoch_is_reported(conn, db_path):
    epoch = finished_session(conn, db_path)
    conn.execute("INSERT INTO effects (session_id, step, tool, epoch, session_epoch) VALUES ('s1', 2, 'lookup', ?, ?)",
                 (epoch + 5, epoch + 5))
    assert any("beyond the session's" in p for p in invariants.check(conn))


def test_effect_committed_by_a_caller_that_was_no_longer_current_is_reported(conn, db_path):
    epoch = finished_session(conn, db_path)
    conn.execute("INSERT INTO effects (session_id, step, tool, epoch, session_epoch) VALUES ('s1', 2, 'lookup', ?, ?)",
                 (epoch, epoch + 1))
    assert any("committed while the session was at epoch" in p for p in invariants.check(conn))


def test_tool_call_without_an_outcome_is_reported(conn):
    state_table.create_session(conn, "s1", "t", step_cap=5)
    epoch = state_table.assign(conn, "s1", "ex-1")
    call = {"do": "tool", "tool": "lookup", "args": {}}
    session_log.append(conn, "s1", epoch, "model", {"action": call})
    session_log.append(conn, "s1", epoch, "model", {"action": call})  # decided again with nothing recorded
    assert any("has no outcome" in p for p in invariants.check(conn))


def test_outcome_for_the_wrong_step_is_reported(conn):
    state_table.create_session(conn, "s1", "t", step_cap=5)
    epoch = state_table.assign(conn, "s1", "ex-1")
    session_log.append(conn, "s1", epoch, "model", {"action": {"do": "tool", "tool": "lookup", "args": {}}})
    session_log.append(conn, "s1", epoch, "tool_result", {"step": 7, "result": {}})
    assert any("outcome for step 7" in p for p in invariants.check(conn))


@pytest.mark.parametrize("state, reason, expect_problem", [("stopped", None, True), ("stopped", "step_cap", False)])
def test_stopped_session_must_say_why(conn, db_path, state, reason, expect_problem):
    finished_session(conn, db_path)
    conn.execute("UPDATE sessions SET state = ?, stop_reason = ? WHERE id = 's1'", (state, reason))
    assert bool(invariants.check(conn)) is expect_problem


def test_ended_session_still_holding_an_executor_is_reported(conn, db_path):
    finished_session(conn, db_path)
    conn.execute("UPDATE sessions SET executor_id = 'ex-1' WHERE id = 's1'")
    assert any("still holds executor" in p for p in invariants.check(conn))


def test_step_count_that_disagrees_with_the_log_is_reported(conn, db_path):
    finished_session(conn, db_path)
    conn.execute("UPDATE sessions SET step_count = 3 WHERE id = 's1'")
    assert any("step_count is 3 but the log has 1" in p for p in invariants.check(conn))
