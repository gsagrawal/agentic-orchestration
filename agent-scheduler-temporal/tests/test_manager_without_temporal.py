"""The session manager and API run against an in-memory SessionEngine: no Temporal anywhere.

This is what the engine interface is for. If these tests ever need Temporal, the manager has
started depending on it again.
"""
import httpx
import pytest

from agent_scheduler_temporal.api import create_app
from agent_scheduler_temporal.engine import EngineUnavailable, SessionStatus
from agent_scheduler_temporal.session_manager import Quotas, SessionManager


class InMemoryEngine:
    """FAKED engine: sessions are dict entries; tests flip their state by hand."""

    def __init__(self):
        self.sessions: dict[str, dict] = {}
        self.fail_start = False
        self.unreachable = False

    async def start(self, session_id, tenant, agent, message):
        if self.fail_start:
            raise RuntimeError("engine down")
        self.sessions[session_id] = {"status": SessionStatus.RUNNING, "open_step": None, "agent": agent, "log": []}

    async def status(self, session_id):
        return self.sessions[session_id]["status"] if session_id in self.sessions else SessionStatus.NOT_FOUND

    async def open_step(self, session_id):
        if self.unreachable:
            raise EngineUnavailable("no answer")
        return self.sessions[session_id]["open_step"]

    async def reply(self, session_id, step, text):
        self.sessions[session_id]["log"].append(("reply", step, text))

    async def message(self, session_id, message_id, text):
        self.sessions[session_id]["log"].append(("message", message_id, text))

    async def stop(self, session_id):
        self.sessions[session_id]["log"].append(("stop",))


@pytest.fixture
def engine():
    e = InMemoryEngine()
    return e


@pytest.fixture
def api(engine, tmp_path):
    manager = SessionManager(engine, str(tmp_path / "m.db"), Quotas(default_max_active=2))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(manager)), base_url="http://api")


def H(tenant="acme", user="u1"):
    return {"X-Tenant": tenant, "X-User": user}


async def start(api, tenant="acme", user="u1", agent="asker"):
    return await api.post("/sessions", json={"agent_id": agent, "message": "hi"}, headers=H(tenant, user))


async def test_quota_is_per_tenant_and_a_slot_is_freed_when_the_engine_says_the_session_ended(api, engine):
    first = (await start(api)).json()["session_id"]
    await start(api)
    assert (await start(api)).status_code == 429
    assert (await start(api, "globex")).status_code == 201  # another tenant is unaffected
    engine.sessions[first]["status"] = SessionStatus.ENDED
    assert (await start(api)).status_code == 201


async def test_the_agent_version_is_handed_to_the_engine(api, engine):
    sid = (await start(api, agent="asker")).json()["session_id"]
    assert engine.sessions[sid]["agent"].agent_id == "asker"


async def test_a_failed_start_gives_the_quota_slot_back(api, engine):
    engine.fail_start = True
    with pytest.raises(RuntimeError):
        await start(api)
    engine.fail_start = False
    assert (await start(api)).status_code == 201 and (await start(api)).status_code == 201


async def test_reply_message_and_stop_reach_the_engine_only_when_allowed(api, engine):
    sid = (await start(api, user="alice")).json()["session_id"]
    engine.sessions[sid]["open_step"] = 3
    body = {"step": 3, "text": "yes"}

    def post(path, payload=None, **who):
        return api.post(f"/sessions/{sid}/{path}", json=payload, headers=H(**who))

    assert (await post("replies", body, tenant="globex", user="alice")).status_code == 404
    assert (await post("replies", body, user="mallory")).status_code == 403
    assert (await post("replies", {"step": 9, "text": "x"}, user="alice")).status_code == 409
    assert engine.sessions[sid]["log"] == []  # nothing got through

    assert (await post("replies", body, user="alice")).status_code == 202
    assert (await post("messages", {"message_id": "m1", "text": "hi"}, user="alice")).status_code == 202
    assert (await post("stop", None, user="alice")).status_code == 202
    assert engine.sessions[sid]["log"] == [("reply", 3, "yes"), ("message", "m1", "hi"), ("stop",)]


async def test_ended_session_is_gone_and_unreachable_engine_is_503(api, engine):
    sid = (await start(api)).json()["session_id"]
    engine.sessions[sid]["open_step"] = 0
    engine.unreachable = True
    assert (await api.post(f"/sessions/{sid}/replies", json={"step": 0, "text": "x"}, headers=H())).status_code == 503
    engine.sessions[sid]["status"] = SessionStatus.ENDED
    assert (await api.post(f"/sessions/{sid}/stop", headers=H())).status_code == 410


async def test_a_session_the_engine_never_heard_of_does_not_block_the_tenant(api, engine, tmp_path):
    import sqlite3

    conn = sqlite3.connect(tmp_path / "m.db")  # the manager died after inserting, before starting
    conn.execute("INSERT INTO sessions VALUES ('acme-ghost', 'acme', 'u1', 1)")
    conn.commit()
    conn.close()
    assert (await start(api)).status_code == 201
    assert (await start(api)).status_code == 201  # both slots usable: the ghost was released
