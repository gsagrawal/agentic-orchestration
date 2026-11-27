"""The invariant checker must be able to fail: feed it states that break each rule."""
import pytest

from agent_scheduler_temporal.chaos import CHAOS_AGENTS
from agent_scheduler_temporal.invariants import check_invariants
from agent_scheduler_temporal.models import SessionResult
from agent_scheduler_temporal.notifier import FakeNotifier
from agent_scheduler_temporal.sandbox import FakeSandboxController
from agent_scheduler_temporal.tool_service import ToolService

MAIL = CHAOS_AGENTS["non-idempotent"]


@pytest.fixture
def stores(tmp_path):
    return ToolService(str(tmp_path / "t.db")), FakeSandboxController(str(tmp_path / "s.db")), FakeNotifier(str(tmp_path / "n.db"))


def result(status="finished", steps=1, tool_status="ok", tool="send_email"):
    return SessionResult(status, steps, [{"role": "tool", "step": 0, "tool": tool, "status": tool_status}])


def check(stores, results, agent=MAIL):
    tools, sandbox, notifier = stores
    return check_invariants(results, {sid: agent for sid in results}, tools, sandbox, notifier)


def test_a_clean_run_passes(stores):
    stores[0].ledger.record("s:0", 1, "send_email")
    assert check(stores, {"s": result()}) == []


def test_duplicate_effect_is_reported(stores):
    stores[0].ledger.record("s:0", 1, "send_email")
    stores[0].ledger.record("s:0", 2, "send_email")
    assert any("2 effects" in v for v in check(stores, {"s": result()}))


def test_ok_result_without_an_effect_is_reported(stores):
    assert any("result ok but 0 effects" in v for v in check(stores, {"s": result()}))


def test_effect_nobody_recorded_is_reported(stores):
    stores[0].ledger.record("ghost:3", 1, "send_email")
    assert any("effect with no tool result" in v for v in check(stores, {}))


def test_outcome_unknown_for_an_idempotent_tool_is_reported(stores):
    assert any("outcome_unknown" in v for v in check(stores, {"s": result(tool_status="outcome_unknown", tool="set_value")}))


def test_unexpected_end_state_and_leaked_sandbox_are_reported(stores):
    stores[1].ensure("s")
    stores[0].ledger.record("s:0", 1, "send_email")
    bad = check(stores, {"s": result(status="stopped")})
    assert any("ended as 'stopped'" in v for v in bad) and any("sandbox still live" in v for v in bad)
