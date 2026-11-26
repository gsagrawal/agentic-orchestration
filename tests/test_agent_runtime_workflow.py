"""Step 2 tests: the AgentSession workflow in the Temporal test environment (time skipping)."""
import uuid

import pytest
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from agent_runtime.activities import Activities
from agent_runtime.models import TASK_QUEUE, AgentVersion, SessionInput
from agent_runtime.sandbox import FakeSandboxController
from agent_runtime.tool_service import ToolService
from agent_runtime.tools import FakeTools
from agent_runtime.workflow import AgentSession


def tool(name, **args):
    return {"do": "tool", "tool": name, "args": args}


def agent(script, grants=("read", "write", "code", "email"), step_cap=20, loop=False):
    return AgentVersion("a", 1, list(FakeTools.SPECS.values()), list(grants), step_cap, script, loop)


@pytest.fixture(scope="session")
async def env():
    e = await WorkflowEnvironment.start_time_skipping()
    yield e
    await e.shutdown()


@pytest.fixture
def tool_service(tmp_path):
    return ToolService(str(tmp_path / "tools.db"))


@pytest.fixture
def sandbox(tmp_path):
    return FakeSandboxController(str(tmp_path / "sandbox.db"))


@pytest.fixture
async def run_session(env, tool_service, sandbox):
    """Runs one session to its end on an in-process worker; returns (result, handle)."""
    async def run(agent_version):
        session_id = f"s-{uuid.uuid4().hex[:8]}"
        acts = Activities(tool_service, sandbox)
        async with Worker(env.client, task_queue=TASK_QUEUE + session_id, workflows=[AgentSession], activities=acts.all):
            handle = await env.client.start_workflow(
                AgentSession.run,
                SessionInput(session_id, "tenant-1", agent_version, "hello"),
                id=session_id,
                task_queue=TASK_QUEUE + session_id,
            )
            return session_id, await handle.result(), handle

    return run


async def test_normal_run_makes_each_effect_once_and_uses_the_sandbox_once(run_session, tool_service, sandbox):
    script = [tool("lookup", k="a"), tool("run_code", cmd="ls"), tool("run_code", cmd="pwd"), {"do": "finish"}]
    sid, result, _ = await run_session(agent(script))

    assert result.status == "finished"
    assert result.steps == 3
    assert [h["status"] for h in result.history if h["role"] == "tool"] == ["ok", "ok", "ok"]
    assert [tool_service.ledger.count(f"{sid}:{i}") for i in range(3)] == [1, 1, 1]
    # One sandbox created for the two code calls, then stopped at the end.
    assert sandbox.calls(sid) == [("ensure", True), ("ensure", False), ("stop", False)]


async def test_a_session_with_no_code_tool_never_touches_the_sandbox(run_session, sandbox):
    sid, _, _ = await run_session(agent([tool("lookup"), {"do": "finish"}]))
    assert sandbox.calls(sid) == []


async def test_tool_without_the_grant_is_denied_and_has_no_effect(run_session, tool_service):
    script = [tool("send_email", to="x"), tool("lookup"), {"do": "finish"}]
    sid, result, _ = await run_session(agent(script, grants=("read",)))

    tool_results = [h for h in result.history if h["role"] == "tool"]
    assert tool_results[0]["status"] == "denied"
    assert tool_results[1]["status"] == "ok"  # the session carries on
    assert tool_service.ledger.count(f"{sid}:0") == 0


async def test_a_tool_the_agent_version_does_not_list_is_denied(run_session):
    _, result, _ = await run_session(agent([tool("no_such_tool"), {"do": "finish"}]))
    assert [h["status"] for h in result.history if h["role"] == "tool"] == ["denied"]


async def test_step_cap_stops_the_poison_script(run_session, tool_service):
    sid, result, _ = await run_session(agent([tool("lookup")], step_cap=5, loop=True))
    assert result.status == "step_cap"
    assert result.steps == 5
    assert tool_service.ledger.duplicates() == []
    assert tool_service.ledger.count(f"{sid}:4") == 1
    assert tool_service.ledger.count(f"{sid}:5") == 0


async def test_workflow_replays_deterministically(run_session):
    script = [tool("lookup"), tool("run_code"), tool("send_email", to="x"), {"do": "finish"}]
    _, _, handle = await run_session(agent(script, grants=("read", "code")))
    # Replay raises if the workflow code takes a different path from its recorded history.
    await Replayer(workflows=[AgentSession]).replay_workflow(await handle.fetch_history())
