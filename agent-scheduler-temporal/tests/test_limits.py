"""Step 7: the two timers, the step cap, and continue-as-new (Temporal test env, time skipping)."""
from datetime import timedelta

import pytest
from temporalio.client import WorkflowExecutionStatus, WorkflowFailureError
from temporalio.exceptions import TimeoutError as TemporalTimeoutError

from agent_scheduler_temporal.models import AgentVersion, MessageSignal, SessionInput
from agent_scheduler_temporal.tools import FakeTools
from agent_scheduler_temporal.workflow import AgentSession
from tests.helpers import System, wait_until


def tool(name, **args):
    return {"do": "tool", "tool": name, "args": args}


async def start_direct(s, agent: AgentVersion, session_id: str, **kwargs):
    """Start a workflow with an agent version built in the test (the manager loads from agents.json)."""
    return await s.env.client.start_workflow(
        AgentSession.run, SessionInput(session_id, "acme", agent, "go"),
        id=session_id, task_queue=s.task_queue, **kwargs,
    )


def build(script, **kw) -> AgentVersion:
    base = dict(agent_id="t", version=1, tools=list(FakeTools.SPECS.values()), grants=["read", "write", "code", "email"],
                step_cap=50, script=script)
    return AgentVersion(**{**base, **kw})


# ---------------------------------------------------------------- waiting expiry

async def test_waiting_session_expires_on_the_waiting_timer(env, tmp_path):
    async with System(env, tmp_path, "q-s7-a") as s:
        sid = await s.start("asker")  # waits for a human, wait_expiry_s = 3600, nobody replies
        await wait_until(lambda: s._is(sid, 0))
        await s.skip(1801)  # past the 1800 s waiting expiry
        result = await s.handle(sid).result()
        assert result.status == "expired"
        assert s.notifier.expired() == [sid]
        assert [m["role"] for m in result.history] == ["user", "assistant"]  # it never went on


async def test_waiting_sessions_hold_no_worker(env, tmp_path):
    # One activity slot. If a waiting session held a worker slot, a second session could not run.
    async with System(env, tmp_path, "q-s7-b", worker_options={"max_concurrent_activities": 1}) as s:
        waiting = [await s.start("asker") for _ in range(3)]
        for sid in waiting:
            await wait_until(lambda sid=sid: s._is(sid, 0))  # open_step is set before request_human ends,
            await wait_until(lambda sid=sid: s.idle(sid))  # so wait for the notification activity to drain
        busy = await s.start("mailer")
        assert (await s.handle(busy).result()).status == "finished"  # ran while three sessions waited
        for sid in waiting:  # leave nothing running: orphans would stall time skipping in later tests
            await s.post(sid, "stop")
            assert (await s.handle(sid).result()).status == "stopped"


# ---------------------------------------------------------------- active-run limit

async def test_session_past_the_active_run_limit_is_stopped_by_the_execution_timeout(env, tmp_path):
    async with System(env, tmp_path, "q-s7-c") as s:
        sid = await s.start("asker-limited")  # limit 3600 s, wait expiry 7200 s: the limit hits first
        await wait_until(lambda: s._is(sid, 0))
        await s.skip(3601)
        with pytest.raises(WorkflowFailureError) as e:
            await s.handle(sid).result()
        assert isinstance(e.value.cause, TemporalTimeoutError)
        assert (await s.handle(sid).describe()).status == WorkflowExecutionStatus.TIMED_OUT
        assert s.notifier.expired() == []  # it was the other timer


async def test_runaway_active_session_is_stopped_by_the_execution_timeout(env, tmp_path):
    # An active loop that the step cap would not stop in time: cap is huge, limit is 5 s. Each tool
    # call takes real time (0.5 s), so the session is busy, not waiting, when the limit hits.
    async with System(env, tmp_path, "q-s7-d", tool_delay_s=0.5) as s:
        handle = await start_direct(s, build([tool("lookup")], loop=True, step_cap=10**9), "runaway-1",
                                    execution_timeout=timedelta(seconds=5))
        with pytest.raises(WorkflowFailureError) as e:
            await handle.result()
        assert isinstance(e.value.cause, TemporalTimeoutError)


# ---------------------------------------------------------------- continue-as-new

async def test_continue_as_new_carries_history_and_keeps_each_step_exactly_once(env, tmp_path):
    script = [tool("lookup", n=i) for i in range(8)] + [{"do": "finish"}]
    async with System(env, tmp_path, "q-s7-e") as s:
        handle = await start_direct(s, build(script, continue_after_steps=3), "can-1")
        result = await handle.result()  # follows the chain of runs
        assert result.status == "finished"
        assert result.continued == 2  # steps 0-2, 3-5, 6-7
        assert [m["step"] for m in result.history if m["role"] == "tool"] == list(range(8))  # history carried whole
        assert [s_.step for s_ in s.model.seen] == list(range(9))  # no step asked of the model twice
        assert [s.tools.ledger.count(f"can-1:{i}") for i in range(8)] == [1] * 8


async def test_continue_as_new_keeps_the_sandbox_and_the_message_dedupe(env, tmp_path):
    script = [tool("run_code", n=i) for i in range(6)] + [{"do": "finish"}]
    async with System(env, tmp_path, "q-s7-f", tool_delay_s=0.4) as s:
        handle = await start_direct(s, build(script, continue_after_steps=2), "can-2")
        await handle.signal(AgentSession.message, MessageSignal("m1", "hi"))
        await wait_until(lambda: s.tools.state("can-2:3") != "none")  # now in a later run
        await handle.signal(AgentSession.message, MessageSignal("m1", "hi again"))
        result = await handle.result()
        assert result.continued >= 2
        assert [m["content"] for m in result.history if m.get("message_id") == "m1"] == ["hi"]  # dropped after CAN
        # One sandbox for the whole session: created once, stopped once, at the very end.
        assert s.sandbox.calls("can-2").count(("stop", False)) == 1
        assert s.sandbox.calls("can-2").count(("ensure", True)) == 1
        assert not any(m["role"] == "system" for m in result.history)  # never "recreated"
