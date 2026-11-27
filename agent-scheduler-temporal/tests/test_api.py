"""Step 4 tests (and later API tests): the session manager API with admission quotas."""
import httpx
import pytest
from temporalio.worker import Worker

from agent_scheduler_temporal.activities import Activities
from agent_scheduler_temporal.api import create_app
from agent_scheduler_temporal.sandbox import FakeSandboxController
from agent_scheduler_temporal.session_manager import Quotas, SessionManager
from agent_scheduler_temporal.temporal_engine import TemporalEngine
from agent_scheduler_temporal.tool_service import ToolService
from agent_scheduler_temporal.workflow import AgentSession


@pytest.fixture
def make_api(env, tmp_path):
    def make(quotas: Quotas, task_queue: str):
        manager = SessionManager(TemporalEngine(env.client, task_queue), str(tmp_path / "sessions.db"), quotas)
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(manager)), base_url="http://api")

    return make


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


async def test_a_quota_row_whose_workflow_never_started_does_not_block_the_tenant(make_api, tmp_path):
    import sqlite3

    api = make_api(Quotas(default_max_active=1), "q-step4-orphan")
    # The manager died after inserting the row and before starting the workflow.
    conn = sqlite3.connect(tmp_path / "sessions.db")
    conn.execute("INSERT INTO sessions VALUES ('acme-ghost', 'acme', 'u1', 1)")
    conn.commit()
    conn.close()
    assert (await start(api, "acme")).status_code == 201  # the dead row is released, not an error


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

from agent_scheduler_temporal.models import ReplySignal
from tests.helpers import System, start, wait_until


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


async def test_reply_when_no_worker_can_answer_is_503_not_a_hang(env, tmp_path):
    s = System(env, tmp_path, "q-s5-h", query_timeout_s=0.5)  # note: the worker is never started
    sid = await s.start("asker")
    r = await s.post(sid, "replies", {"step": 0, "text": "x"})
    assert r.status_code == 503


async def test_reply_after_the_waiting_timer_expired_is_gone(env, tmp_path):
    async with System(env, tmp_path, "q-s5-i") as s:
        sid = await s.start("asker")  # wait_expiry_s = 1800
        await wait_until(lambda: s._is(sid, 0))
        await s.skip(1801)
        assert (await s.handle(sid).result()).status == "expired"
        assert (await s.post(sid, "replies", {"step": 0, "text": "too late"})).status_code == 410


async def test_the_agent_version_is_pinned_for_the_session(env, tmp_path):
    import json
    import shutil

    from agent_scheduler_temporal.agent_store import DEFAULT_PATH

    agents = tmp_path / "agents.json"
    shutil.copy(DEFAULT_PATH, agents)
    async with System(env, tmp_path, "q-s5-j", agents_path=agents) as s:
        sid = await s.start("asker")  # step 1 of its script is a lookup
        await wait_until(lambda: s._is(sid, 0))
        data = json.loads(agents.read_text())  # the agent is edited while the session waits
        data["asker"]["1"]["script"][1] = {"do": "tool", "tool": "send_email", "args": {}}
        agents.write_text(json.dumps(data))
        await s.post(sid, "replies", {"step": 0, "text": "yes"})
        result = await s.handle(sid).result()
        assert [m["tool"] for m in result.history if m["role"] == "tool"] == ["lookup"]  # the old version

