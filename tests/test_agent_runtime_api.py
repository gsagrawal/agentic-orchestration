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
