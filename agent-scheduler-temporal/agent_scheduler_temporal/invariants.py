"""End-of-run checks for the chaos test and the benchmark.

check_invariants returns a list of violations; empty means every invariant held.
"""
from agent_scheduler_temporal.agent import AgentVersion
from agent_scheduler_temporal.models import SessionResult
from agent_scheduler_temporal.notifier import FakeNotifier
from agent_scheduler_temporal.sandbox import FakeSandboxController
from agent_scheduler_temporal.tool_service import ToolService
from agent_scheduler_temporal.tools import FakeTools

END_STATES = {"finished", "step_cap", "expired"}  # a chaos session is never stopped or timed out


def check_invariants(
    results: dict[str, SessionResult],
    agents: dict[str, AgentVersion],
    tools: ToolService,
    sandbox: FakeSandboxController,
    notifier: FakeNotifier,
) -> list[str]:
    bad: list[str] = []
    counts = tools.ledger.counts()

    # 1. Every session ended in an allowed end state.
    for sid, r in results.items():
        if r.status not in END_STATES:
            bad.append(f"{sid}: ended as {r.status!r}")
        if r.status == "step_cap" and r.steps != agents[sid].step_cap:
            bad.append(f"{sid}: step cap {agents[sid].step_cap} but ran {r.steps} steps")

    # 2. No (session, step) has more than one effect.
    for key in tools.ledger.duplicates():
        bad.append(f"{key}: {counts[key]} effects")

    # 3. What the model was told matches what really happened.
    seen_keys = set()
    for sid, r in results.items():
        for m in (m for m in r.history if m["role"] == "tool"):
            key = f"{sid}:{m['step']}"
            seen_keys.add(key)
            n = counts.get(key, 0)
            idempotent = FakeTools.SPECS[m["tool"]].idempotent
            if m["status"] == "ok" and n != 1:
                bad.append(f"{key}: result ok but {n} effects")
            elif m["status"] == "outcome_unknown" and (idempotent or n > 1):
                bad.append(f"{key}: outcome_unknown for idempotent tool or {n} effects")
            elif m["status"] == "denied" and n != 0:
                bad.append(f"{key}: denied but {n} effects")
    for key in set(counts) - seen_keys:
        bad.append(f"{key}: effect with no tool result in any history")  # an effect nobody recorded

    # 4. Nothing left behind: no live sandbox, and expiry marks match the sessions that expired.
    for sid in sandbox.live_sessions():
        bad.append(f"{sid}: sandbox still live")
    expired = {sid for sid, r in results.items() if r.status == "expired"}
    if set(notifier.expired()) != expired:
        bad.append(f"expired marks {sorted(notifier.expired())} != expired sessions {sorted(expired)}")
    return bad
