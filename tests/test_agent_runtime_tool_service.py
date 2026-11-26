"""Step 1 tests: idempotency keys, fencing, started markers, effects ledger."""
import threading

import pytest

from agent_runtime.tool_service import OK, OUTCOME_UNKNOWN, StaleAttempt, ToolService
from agent_runtime.tools import FakeTools


class Crash(Exception):
    """Stands in for the worker dying at a chosen point inside a tool call."""


class CrashingTools(FakeTools):
    """Crashes `before` the effect, or `after` it, on the first run only."""

    def __init__(self, when: str):
        super().__init__()
        self.when = when
        self.crashed = False

    def run(self, ledger, spec, key, attempt, args):
        if self.crashed:
            return super().run(ledger, spec, key, attempt, args)
        self.crashed = True
        if self.when == "before":
            raise Crash
        super().run(ledger, spec, key, attempt, args)  # the effect happens...
        raise Crash  # ...but the result is never recorded


@pytest.fixture
def path(tmp_path):
    return str(tmp_path / "tools.db")


def test_same_key_twice_gives_one_effect_and_same_result(path):
    svc = ToolService(path)
    first = svc.execute("s1:1", 1, "send_email", {"to": "a"})
    second = svc.execute("s1:1", 1, "send_email", {"to": "a"})
    assert first.status == second.status == OK
    assert first.value == second.value
    assert svc.ledger.count("s1:1") == 1


def test_lower_attempt_is_rejected_and_causes_no_effect(path):
    svc = ToolService(path)
    svc.execute("s1:1", 2, "send_email", {})  # the new worker, attempt 2
    with pytest.raises(StaleAttempt):
        svc.execute("s1:1", 1, "send_email", {})  # zombie, attempt 1
    assert svc.ledger.count("s1:1") == 1


def test_lower_attempt_is_rejected_even_for_a_key_with_no_result(path):
    svc = ToolService(path, tools=CrashingTools("before"))
    with pytest.raises(Crash):
        svc.execute("s1:1", 2, "send_email", {})
    with pytest.raises(StaleAttempt):
        svc.execute("s1:1", 1, "send_email", {})
    assert svc.ledger.count("s1:1") == 0


def test_started_marker_without_result_is_reported(path):
    svc = ToolService(path, tools=CrashingTools("before"))
    assert svc.state("s1:1") == "none"
    with pytest.raises(Crash):
        svc.execute("s1:1", 1, "set_value", {})
    assert svc.state("s1:1") == "started"
    svc.execute("s1:1", 2, "set_value", {})  # the retry finishes it
    assert svc.state("s1:1") == "done"


@pytest.mark.parametrize("when", ["before", "after"])
def test_idempotent_tool_is_rerun_and_gives_exactly_one_effect(path, when):
    svc = ToolService(path, tools=CrashingTools(when))
    with pytest.raises(Crash):
        svc.execute("s1:1", 1, "set_value", {})
    result = svc.execute("s1:1", 2, "set_value", {})
    assert result.status == OK
    assert svc.ledger.count("s1:1") == 1
    assert svc.ledger.duplicates() == []


@pytest.mark.parametrize("when,effects", [("before", 0), ("after", 1)])
def test_non_idempotent_tool_with_marker_and_no_result_is_outcome_unknown(path, when, effects):
    svc = ToolService(path, tools=CrashingTools(when))
    with pytest.raises(Crash):
        svc.execute("s1:1", 1, "send_email", {})
    result = svc.execute("s1:1", 2, "send_email", {})
    assert result.status == OUTCOME_UNKNOWN
    # Not retried: the ledger is exactly what the first run left. The marker alone
    # cannot say which of the two cases this was, which is why the model is told "unknown".
    assert svc.ledger.count("s1:1") == effects


def test_non_idempotent_tool_with_no_marker_is_run(path):
    svc = ToolService(path)
    assert svc.state("s1:1") == "none"
    assert svc.execute("s1:1", 1, "send_email", {}).status == OK
    assert svc.ledger.count("s1:1") == 1


def test_zombie_cannot_record_a_result_after_a_newer_attempt_took_over(path):
    class TakeoverTools(FakeTools):
        """While attempt 1 is still running, attempt 2 starts and finishes."""

        def run(self, ledger, spec, key, attempt, args):
            if attempt == 1:
                svc.execute(key, 2, spec.name, args)
            return super().run(ledger, spec, key, attempt, args)

    svc = ToolService(path, tools=TakeoverTools())
    with pytest.raises(StaleAttempt):
        svc.execute("s1:1", 1, "set_value", {})
    assert svc.state("s1:1") == "done"
    assert svc.ledger.count("s1:1") == 1


@pytest.mark.parametrize("tool", ["set_value", "send_email"])
def test_concurrent_calls_with_the_same_key_give_one_effect(path, tool):
    svc = ToolService(path, tools=FakeTools(delay_s=0.1))
    results = []
    threads = [threading.Thread(target=lambda: results.append(svc.execute("s1:1", 1, tool, {}))) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert svc.ledger.count("s1:1") == 1
    if tool == "send_email":  # one runs; the rest see the marker and must not run it
        assert sorted(r.status for r in results).count(OK) == 1
