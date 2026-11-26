import asyncio

import httpx
from temporalio.worker import Worker

from agent_scheduler_temporal.activities import Activities
from agent_scheduler_temporal.api import create_app
from agent_scheduler_temporal.notifier import FakeNotifier
from agent_scheduler_temporal.sandbox import FakeSandboxController
from agent_scheduler_temporal.scripted_model import ScriptedModel
from agent_scheduler_temporal.session_manager import Quotas, SessionManager
from agent_scheduler_temporal.tool_service import ToolService
from agent_scheduler_temporal.tools import FakeTools
from agent_scheduler_temporal.workflow import AgentSession


async def wait_until(check, timeout=20.0, interval=0.05):
    """Polls an async or sync condition; fails the test on timeout instead of hanging."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        value = check()
        if asyncio.iscoroutine(value):
            value = await value
        if value:
            return value
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(interval)


from temporalio.api.enums.v1 import EventType
from temporalio.converter import DataConverter


async def model_inputs(handle) -> list[dict]:
    """What the model was shown, read from the workflow's recorded history (works across processes)."""
    inputs = []
    async for event in handle.fetch_history_events():
        if event.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED:
            attrs = event.activity_task_scheduled_event_attributes
            if attrs.activity_type.name == "call_model":
                [req] = await DataConverter.default.decode(attrs.input.payloads)
                inputs.append(req)
    return inputs


def start(api, tenant, user="u1", agent="researcher"):
    return api.post("/sessions", json={"agent_id": agent, "message": "hi"}, headers={"X-Tenant": tenant, "X-User": user})


class System:
    """A manager + API + in-process worker over one tmp dir, with an inspectable model."""

    def __init__(self, env, tmp_path, task_queue, tool_delay_s=0.0, quotas=None, worker_options=None,
                 agents_path=None, query_timeout_s=5.0):
        self.env = env
        self.task_queue = task_queue
        self.tools = ToolService(str(tmp_path / "t.db"), tools=FakeTools(delay_s=tool_delay_s))
        self.notifier = FakeNotifier(str(tmp_path / "n.db"))
        self.model = ScriptedModel(record=True)
        self.sandbox = FakeSandboxController(str(tmp_path / "s.db"))
        acts = Activities(self.tools, self.sandbox, self.notifier, self.model)
        self.worker = Worker(env.client, task_queue=task_queue, workflows=[AgentSession], activities=acts.all, **(worker_options or {}))
        manager = SessionManager(env.client, str(tmp_path / "m.db"), quotas or Quotas(default_max_active=50), task_queue=task_queue,
                                  **({"agents_path": agents_path} if agents_path else {}), query_timeout_s=query_timeout_s)
        self.api = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(manager)), base_url="http://api")

    async def __aenter__(self):
        await self.worker.__aenter__()
        return self

    async def __aexit__(self, *exc):
        await self.worker.__aexit__(*exc)

    async def start(self, agent, tenant="acme", user="u1") -> str:
        r = await start(self.api, tenant, user, agent)
        assert r.status_code == 201, r.text
        return r.json()["session_id"]

    async def skip(self, seconds: float):
        """Jump the test server's clock. Needed because the manager starts the workflow, and time
        skipping only unlocks by itself for the handle that start_workflow returned."""
        await self.env.sleep(seconds)

    def handle(self, sid):
        return self.env.client.get_workflow_handle_for(AgentSession.run, sid)

    async def idle(self, sid) -> bool:
        """True when the session has no activity running, i.e. it holds no worker."""
        return not (await self.handle(sid).describe()).raw_description.pending_activities

    async def _is(self, sid, step):
        return await self.handle(sid).query(AgentSession.open_step) == step

    def post(self, sid, path, body=None, tenant="acme", user="u1"):
        return self.api.post(f"/sessions/{sid}/{path}", json=body, headers={"X-Tenant": tenant, "X-User": user})


