"""Step 4 tests (and later API tests): the session manager API with admission quotas."""
import httpx
import pytest
from temporalio.worker import Worker

from agent_runtime.activities import Activities
from agent_runtime.api import create_app
from agent_runtime.models import TASK_QUEUE
from agent_runtime.sandbox import FakeSandboxController
from agent_runtime.session_manager import Quotas, SessionManager
from agent_runtime.tool_service import ToolService
from agent_runtime.workflow import AgentSession


@pytest.fixture
def make_api(env, tmp_path):
    def make(quotas: Quotas, task_queue: str):
        manager = SessionManager(env.client, str(tmp_path / "sessions.db"), quotas, task_queue=task_queue)
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(manager)), base_url="http://api")

    return make


def start(api, tenant, user="u1", agent="researcher"):
    return api.post("/sessions", json={"agent_id": agent, "message": "hi"}, headers={"X-Tenant": tenant, "X-User": user})


async def test_tenant_over_quota_is_refused_and_other_tenants_are_not_affected(make_api):
    # No worker runs, so every started session stays running and keeps its quota slot.
    api = make_api(Quotas(default_max_active=2, per_tenant={"small": 1}), "q-step4-a")
    assert (await start(api, "acme")).status_code == 201
    assert (await start(api, "acme")).status_code == 201
    over = await start(api, "acme")
    assert over.status_code == 429
    assert (await start(api, "globex")).status_code == 201  # another tenant: unaffected
    assert (await start(api, "small")).status_code == 201
    assert (await start(api, "small")).status_code == 429  # per-tenant limit applies


async def test_unknown_agent_is_404_and_takes_no_quota(make_api):
    api = make_api(Quotas(default_max_active=1), "q-step4-b")
    assert (await start(api, "acme", agent="nope")).status_code == 404
    assert (await start(api, "acme")).status_code == 201


async def test_quota_slot_is_freed_when_a_session_finishes(env, make_api, tmp_path):
    api = make_api(Quotas(default_max_active=1), "q-step4-c")
    first = await start(api, "acme")
    assert first.status_code == 201
    assert (await start(api, "acme")).status_code == 429
    acts = Activities(ToolService(str(tmp_path / "t.db")), FakeSandboxController(str(tmp_path / "s.db")))
    async with Worker(env.client, task_queue="q-step4-c", workflows=[AgentSession], activities=acts.all):
        await env.client.get_workflow_handle(first.json()["session_id"]).result()
        assert (await start(api, "acme")).status_code == 201


# ---------------------------------------------------------------- step 5: human, messages, stop

from agent_runtime.models import MessageSignal, ReplySignal
from agent_runtime.notifier import FakeNotifier
from agent_runtime.scripted_model import ScriptedModel
from agent_runtime.tools import FakeTools
from tests.helpers_runtime import wait_until


class System:
    """A manager + API + in-process worker over one tmp dir, with an inspectable model."""

    def __init__(self, env, tmp_path, task_queue, tool_delay_s=0.0, quotas=None):
        self.env = env
        self.task_queue = task_queue
        self.tools = ToolService(str(tmp_path / "t.db"), tools=FakeTools(delay_s=tool_delay_s))
        self.notifier = FakeNotifier(str(tmp_path / "n.db"))
        self.model = ScriptedModel()
        self.sandbox = FakeSandboxController(str(tmp_path / "s.db"))
        acts = Activities(self.tools, self.sandbox, self.notifier, self.model)
        self.worker = Worker(env.client, task_queue=task_queue, workflows=[AgentSession], activities=acts.all)
        manager = SessionManager(env.client, str(tmp_path / "m.db"), quotas or Quotas(default_max_active=50), task_queue=task_queue)
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

    def handle(self, sid):
        return self.env.client.get_workflow_handle_for(AgentSession.run, sid)

    async def _is(self, sid, step):
        return await self.handle(sid).query(AgentSession.open_step) == step

    def post(self, sid, path, body=None, tenant="acme", user="u1"):
        return self.api.post(f"/sessions/{sid}/{path}", json=body, headers={"X-Tenant": tenant, "X-User": user})


async def test_reply_for_the_open_step_wakes_the_session(env, tmp_path):
    async with System(env, tmp_path, "q-s5-a") as s:
        sid = await s.start("asker")
        await wait_until(lambda: s._is(sid, 0))
        assert s.notifier.sent() == [f"{sid}:0"]  # notified once, and no activity is holding a worker
        r = await s.post(sid, "replies", {"step": 0, "text": "yes"})
        assert r.status_code == 202
        result = await s.handle(sid).result()
        assert result.status == "finished"
        assert {"role": "human", "step": 0, "content": "yes"} in result.history


async def test_reply_for_another_step_is_refused_and_a_duplicate_is_ignored(env, tmp_path):
    async with System(env, tmp_path, "q-s5-b") as s:
        sid = await s.start("asker")
        await wait_until(lambda: s._is(sid, 0))
        wrong = await s.post(sid, "replies", {"step": 7, "text": "wrong"})
        assert wrong.status_code == 409
        # Signals straight to the workflow: the workflow itself drops what is not for the open step.
        h = s.handle(sid)
        await h.signal(AgentSession.reply, ReplySignal(3, "other step"))
        await h.signal(AgentSession.reply, ReplySignal(0, "first"))
        await h.signal(AgentSession.reply, ReplySignal(0, "duplicate"))
        result = await h.result()
        humans = [m for m in result.history if m["role"] == "human"]
        assert humans == [{"role": "human", "step": 0, "content": "first"}]


async def test_reply_checks_tenant_and_responder(env, tmp_path):
    async with System(env, tmp_path, "q-s5-c") as s:
        sid = await s.start("asker", tenant="acme", user="alice")
        await wait_until(lambda: s._is(sid, 0))
        body = {"step": 0, "text": "x"}
        assert (await s.post(sid, "replies", body, tenant="globex", user="alice")).status_code == 404
        assert (await s.post(sid, "replies", body, tenant="acme", user="mallory")).status_code == 403
        assert (await s.handle(sid).query(AgentSession.open_step)) == 0  # neither reached the workflow
        assert (await s.post(sid, "replies", body, tenant="acme", user="alice")).status_code == 202


async def test_reply_after_the_session_ended_is_gone(env, tmp_path):
    async with System(env, tmp_path, "q-s5-d") as s:
        sid = await s.start("asker")
        await wait_until(lambda: s._is(sid, 0))
        await s.post(sid, "replies", {"step": 0, "text": "yes"})
        await s.handle(sid).result()
        assert (await s.post(sid, "replies", {"step": 0, "text": "again"})).status_code == 410


async def test_message_during_a_tool_call_is_seen_at_the_next_step_and_does_not_interrupt(env, tmp_path):
    async with System(env, tmp_path, "q-s5-e", tool_delay_s=1.0) as s:
        sid = await s.start("mailer")  # step 0: send_email (slow, not idempotent)
        await wait_until(lambda: s.tools.state(f"{sid}:0") == "started")  # the tool call is in flight
        for _ in range(2):  # the second post has the same id: dropped
            r = await s.post(sid, "messages", {"message_id": "m1", "text": "also check X"})
            assert r.status_code == 202
        result = await s.handle(sid).result()

        assert result.status == "finished"
        assert s.tools.ledger.count(f"{sid}:0") == 1  # the call finished normally, once
        kinds = [(m["role"], m.get("step")) for m in result.history]
        assert kinds.index(("tool", 0)) < [m.get("message_id") for m in result.history].index("m1")
        assert [m for m in result.history if m.get("message_id") == "m1"] == [
            {"role": "user", "message_id": "m1", "content": "also check X"}
        ]
        # What the model was shown: step 0 had no message; the step-1 request has it exactly once.
        by_step = {r.step: r for r in s.model.seen}
        assert not any(m.get("message_id") for m in by_step[0].history)
        assert sum(1 for m in by_step[1].history if m.get("message_id") == "m1") == 1


async def test_stop_ends_the_session_at_the_next_step_boundary(env, tmp_path):
    async with System(env, tmp_path, "q-s5-f", tool_delay_s=1.0) as s:
        sid = await s.start("mailer")
        await wait_until(lambda: s.tools.state(f"{sid}:0") == "started")
        assert (await s.post(sid, "stop")).status_code == 202
        result = await s.handle(sid).result()
        assert result.status == "stopped"
        assert result.steps == 1  # the in-flight call completed; no further step started
        assert s.tools.ledger.count(f"{sid}:0") == 1
        assert s.tools.ledger.count(f"{sid}:1") == 0


async def test_stop_while_waiting_for_a_human_ends_the_session(env, tmp_path):
    async with System(env, tmp_path, "q-s5-g") as s:
        sid = await s.start("asker")
        await wait_until(lambda: s._is(sid, 0))
        await s.post(sid, "stop")
        assert (await s.handle(sid).result()).status == "stopped"
