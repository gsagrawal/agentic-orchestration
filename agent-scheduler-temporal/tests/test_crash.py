"""Step 6: a tool call in flight when its worker dies. Real worker processes, real SIGKILL.

Needs the Temporal dev server: `make test-integration`.
Timing: heartbeat timeout is 3 s (models.py), so each recovery takes a few seconds.
"""
import asyncio

import pytest

from agent_scheduler_temporal.agent_store import load_agent
from agent_scheduler_temporal.models import SessionInput
from agent_scheduler_temporal.tool_service import ToolService
from agent_scheduler_temporal.workflow import AgentSession
from tests.helpers import model_inputs, wait_until

pytestmark = pytest.mark.integration

# agent, tool, step-0 status and effect count after recovery, per kill point.
IDEMPOTENT = {  # set_value: re-running is safe, so every point ends with status ok and one effect
    "before_marker": ("ok", 1), "before_effect": ("ok", 1), "after_effect": ("ok", 1), "after_result": ("ok", 1),
}
NON_IDEMPOTENT = {  # send_email
    "before_marker": ("ok", 1),               # no marker: nothing started, so it is safe to run
    "before_effect": ("outcome_unknown", 0),  # marker, no result: not retried (here the effect had not happened)
    "after_effect": ("outcome_unknown", 1),   # marker, no result: not retried (here the effect had happened)
    "after_result": ("ok", 1),                # result stored: returned, not run again
}


async def start_session(dev_env, workers, agent_id, session_id):
    handle = await dev_env.client.start_workflow(
        AgentSession.run,
        SessionInput(session_id, "tenant-1", load_agent(agent_id, 1), "go"),
        id=session_id,
        task_queue=workers.task_queue,
    )
    return handle


async def run_with_fault(dev_env, workers, agent_id, point):
    """Worker A dies at `point`; worker B (started after A died) finishes the session."""
    a = workers.spawn(fault=f"{point}:kill")
    handle = await start_session(dev_env, workers, agent_id, f"crash-{agent_id}-{point}")
    await wait_until(lambda: a.poll() is not None, timeout=30)
    assert a.returncode == -9, "worker A should have been killed by SIGKILL"
    workers.spawn()  # worker B
    result = await asyncio.wait_for(handle.result(), timeout=60)
    return handle, result


@pytest.mark.parametrize("point", IDEMPOTENT)
async def test_idempotent_tool_gives_exactly_one_effect_whenever_the_worker_dies(dev_env, workers, point):
    handle, result = await run_with_fault(dev_env, workers, "setter", point)
    tools = ToolService(f"{workers.data_dir}/tools.db")
    key = f"{handle.id}:0"
    assert result.status == "finished"
    assert [m["status"] for m in result.history if m["role"] == "tool"] == ["ok", "ok"]
    assert tools.ledger.count(key) == 1
    assert tools.ledger.duplicates() == []
    assert max(a for a, _ in tools.attempts(key)) >= 2 or point == "after_result"  # it really was retried


@pytest.mark.parametrize("point", NON_IDEMPOTENT)
async def test_non_idempotent_tool_is_never_run_twice(dev_env, workers, point):
    status, effects = NON_IDEMPOTENT[point]
    handle, result = await run_with_fault(dev_env, workers, "mailer", point)
    tools = ToolService(f"{workers.data_dir}/tools.db")
    assert result.status == "finished"  # the session carries on either way
    assert [m["status"] for m in result.history if m["role"] == "tool"][0] == status
    assert tools.ledger.count(f"{handle.id}:0") == effects
    assert tools.ledger.duplicates() == []
    if status == "outcome_unknown":
        # The model's next input carries the "outcome unknown" result for step 0.
        step1 = [r for r in await model_inputs(handle) if r["step"] == 1][0]
        assert {"role": "tool", "step": 0, "tool": "send_email", "status": "outcome_unknown", "value": None} in step1["history"]


@pytest.mark.parametrize("agent_id,status,effects", [("setter", "ok", 1), ("mailer", "outcome_unknown", 0)])
async def test_kill_minus_9_from_outside_during_a_slow_tool_call(dev_env, workers, agent_id, status, effects):
    """No fault hook: the test itself sends SIGKILL while the tool service shows the call started."""
    a = workers.spawn(tool_delay=30)  # the tool sleeps before its effect, so the kill lands mid-call
    handle = await start_session(dev_env, workers, agent_id, f"outside-{agent_id}")
    tools = ToolService(f"{workers.data_dir}/tools.db")
    await wait_until(lambda: tools.state(f"{handle.id}:0") == "started", timeout=30)
    a.kill()  # SIGKILL
    a.wait()
    workers.spawn()  # worker B, no delay
    result = await asyncio.wait_for(handle.result(), timeout=60)
    assert result.status == "finished"
    assert [m["status"] for m in result.history if m["role"] == "tool"][0] == status
    assert tools.ledger.count(f"{handle.id}:0") == effects
    assert tools.ledger.duplicates() == []


async def test_a_frozen_worker_cannot_cause_a_second_effect_when_it_wakes(dev_env, workers):
    """Worker A freezes (SIGSTOP) before calling the tool service: cut off from Temporal but alive.
    Temporal retries on B (attempt 2), which does the work. When A is resumed, its attempt 1 is
    rejected by the tool service. send_email is NOT idempotent, so only the fence protects it."""
    a = workers.spawn(fault="before_marker:stop")
    handle = await start_session(dev_env, workers, "mailer", "zombie-1")
    await wait_until(lambda: a.fault_file and __import__("os").path.exists(a.fault_file), timeout=30)
    workers.spawn()  # worker B
    result = await asyncio.wait_for(handle.result(), timeout=60)
    tools = ToolService(f"{workers.data_dir}/tools.db")
    assert result.status == "finished"
    assert tools.ledger.count("zombie-1:0") == 1

    a.send_signal(18)  # SIGCONT: the zombie resumes and calls the tool service with attempt 1
    # Every attempt the zombie made is lower than the attempt that did the work, and was rejected.
    # (Usually 1 and 2: the frozen worker's pending poll is handed the retry too, and it cannot run it.)
    winning = max(attempt for attempt, _ in tools.attempts("zombie-1:0"))
    await wait_until(lambda: tools.fenced() and all(k == "zombie-1:0" and n < winning for k, n in tools.fenced()), timeout=30)
    assert 1 in [n for _, n in tools.fenced()]
    assert tools.ledger.count("zombie-1:0") == 1  # still exactly one
    assert tools.ledger.duplicates() == []
