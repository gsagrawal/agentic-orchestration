"""Tool service: idempotency keys, epoch fencing, effects ledger."""

import threading

import pytest

from orchestrator import state_table
from orchestrator.errors import KeyConflict, StaleEpoch
from orchestrator.fake_tools import default_tools
from orchestrator.tool_service import ToolService, effect_counts


@pytest.fixture
def epoch(conn):
    state_table.create_session(conn, "s1", "t1", step_cap=10)
    return state_table.assign(conn, "s1", "ex-A")


def make_service(db_path, hook=None):
    return ToolService(db_path, default_tools(), fault_hook=hook)


@pytest.mark.parametrize("tool", ["lookup", "set_value", "send_email"])
def test_same_key_twice_gives_one_effect_and_the_same_result(conn, db_path, epoch, tool):
    svc = make_service(db_path)
    first = svc.call("s1", 1, epoch, tool, {"key": "k"})
    second = svc.call("s1", 1, epoch, tool, {"key": "k"})
    assert first == second
    assert effect_counts(conn) == {("s1", 1): 1}


def test_different_steps_are_different_effects(conn, db_path, epoch):
    svc = make_service(db_path)
    a = svc.call("s1", 1, epoch, "send_email", {"to": "x"})
    b = svc.call("s1", 2, epoch, "send_email", {"to": "x"})
    assert a != b
    assert effect_counts(conn) == {("s1", 1): 1, ("s1", 2): 1}


def test_retry_from_a_new_epoch_returns_the_stored_result(conn, db_path, epoch):
    svc = make_service(db_path)
    first = svc.call("s1", 1, epoch, "send_email", {"to": "x"})
    new_epoch = state_table.assign(conn, "s1", "ex-B")
    assert svc.lookup("s1", 1, new_epoch) == first
    assert svc.call("s1", 1, new_epoch, "send_email", {"to": "x"}) == first
    assert effect_counts(conn) == {("s1", 1): 1}


def test_lookup_of_a_call_that_never_ran_is_none(db_path, epoch):
    assert make_service(db_path).lookup("s1", 1, epoch) is None


def test_stale_epoch_cannot_cause_an_effect(conn, db_path, epoch):
    svc = make_service(db_path)
    state_table.assign(conn, "s1", "ex-B")
    with pytest.raises(StaleEpoch):
        svc.call("s1", 1, epoch, "send_email", {"to": "x"})
    assert effect_counts(conn) == {}
    with pytest.raises(StaleEpoch):
        svc.lookup("s1", 1, epoch)


def test_zombie_that_passed_the_first_check_is_fenced_at_the_effect(conn, db_path, epoch):
    """The reassignment lands after the zombie's first epoch check, while it is
    'asleep' just before the effect. The re-check inside the effect transaction stops it."""
    def reassign_while_zombie_sleeps(point):
        if point == "before_effect":
            state_table.assign(conn, "s1", "ex-B")

    svc = make_service(db_path, reassign_while_zombie_sleeps)
    with pytest.raises(StaleEpoch):
        svc.call("s1", 1, epoch, "send_email", {"to": "x"})
    assert effect_counts(conn) == {}


def test_after_fencing_the_new_executor_still_gets_exactly_one_effect(conn, db_path, epoch):
    reassigned = []

    def hook(point):
        if point == "before_effect" and not reassigned:
            reassigned.append(state_table.assign(conn, "s1", "ex-B"))

    svc = make_service(db_path, hook)
    with pytest.raises(StaleEpoch):
        svc.call("s1", 1, epoch, "set_value", {"key": "k"})
    svc.call("s1", 1, reassigned[0], "set_value", {"key": "k"})
    assert effect_counts(conn) == {("s1", 1): 1}


def test_non_idempotent_crash_between_effect_and_result_leaves_effect_without_result(conn, db_path, epoch):
    """The window the executor must handle (step 6): effect committed, result never recorded."""
    class Died(Exception):
        pass

    def die_after_effect(point):
        if point == "after_effect":
            raise Died

    svc = make_service(db_path, die_after_effect)
    with pytest.raises(Died):
        svc.call("s1", 1, epoch, "send_email", {"to": "x"})
    assert effect_counts(conn) == {("s1", 1): 1}
    assert make_service(db_path).lookup("s1", 1, epoch) is None


def test_idempotent_tool_has_no_such_window(conn, db_path, epoch):
    seen = []
    svc = make_service(db_path, seen.append)
    svc.call("s1", 1, epoch, "set_value", {"key": "k"})
    assert "after_effect" not in seen


def test_key_reused_for_different_arguments_is_an_error(conn, db_path, epoch):
    svc = make_service(db_path)
    svc.call("s1", 1, epoch, "set_value", {"key": "a"})
    with pytest.raises(KeyConflict):
        svc.call("s1", 1, epoch, "set_value", {"key": "b"})
    with pytest.raises(KeyConflict):
        svc.call("s1", 1, epoch, "lookup", {"key": "a"})


def test_effects_ledger_is_append_only(conn, db_path, epoch):
    make_service(db_path).call("s1", 1, epoch, "lookup", {"key": "k"})
    with pytest.raises(Exception, match="append-only"):
        conn.execute("DELETE FROM effects")


def test_concurrent_calls_with_the_same_key_give_one_effect(conn, db_path, epoch):
    """Six callers (own connection each, as separate processes would have) all pass the
    first lookup during the tool's delay, then race to commit. Only one may run the tool."""
    results, errors = [], []

    def caller():
        try:
            svc = ToolService(db_path, default_tools(delay=0.1))
            results.append(svc.call("s1", 1, epoch, "set_value", {"key": "k"}))
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=caller) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert results == [{"set": "k"}] * 6
    assert effect_counts(conn) == {("s1", 1): 1}


def test_non_idempotent_result_is_recorded_even_if_the_epoch_changed_in_the_window(conn, db_path, epoch):
    """The effect happened, so its result is recorded for the new holder to find (DESIGN_CHOICES #16)."""
    new = []

    def reassign_in_window(point):
        if point == "after_effect":
            new.append(state_table.assign(conn, "s1", "ex-B"))

    svc = make_service(db_path, reassign_in_window)
    result = svc.call("s1", 1, epoch, "send_email", {"to": "x"})
    assert make_service(db_path).lookup("s1", 1, new[0]) == result
    assert effect_counts(conn) == {("s1", 1): 1}


def test_known_limit_call_alone_reruns_a_non_idempotent_tool_that_has_no_stored_result(conn, db_path, epoch):
    """Not a feature: pins the contract that the EXECUTOR must not call again after a crash
    in the window (step 6). The service on its own is not safe here (DESIGN_CHOICES #17)."""
    class Died(Exception):
        pass

    def die_after_effect(point):
        if point == "after_effect":
            raise Died

    with pytest.raises(Died):
        make_service(db_path, die_after_effect).call("s1", 1, epoch, "send_email", {"to": "x"})
    make_service(db_path).call("s1", 1, epoch, "send_email", {"to": "x"})
    assert effect_counts(conn) == {("s1", 1): 2}


def test_tool_that_raises_leaves_no_effect_and_no_result(conn, db_path, epoch):
    from orchestrator.fake_tools import Tool

    def boom(args, effect_id):
        raise RuntimeError("tool failed")

    svc = ToolService(db_path, {"boom": Tool("boom", True, boom)})
    with pytest.raises(RuntimeError):
        svc.call("s1", 1, epoch, "boom", {})
    assert effect_counts(conn) == {}
    assert svc.lookup("s1", 1, epoch) is None
