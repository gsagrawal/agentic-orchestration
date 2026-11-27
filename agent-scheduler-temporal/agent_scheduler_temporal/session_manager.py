"""session-manager: admission, per-tenant quotas, and who may talk to which session.

Admission is checked before the session is started. The manager keeps one small table
(session -> tenant, user, active flag); it holds no call-by-call record. It reaches the thing that
runs sessions only through the SessionEngine interface (engine.py), so it knows nothing about
Temporal; see temporal_engine.py for the Temporal implementation.
"""
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path

from agent_scheduler_temporal.agent_store import DEFAULT_PATH, load_agent
from agent_scheduler_temporal.db import txn
from agent_scheduler_temporal.engine import EngineUnavailable, SessionEngine, SessionStatus

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    tenant     TEXT NOT NULL,
    user       TEXT NOT NULL,           -- who started it; the only allowed responder (assumption)
    active     INTEGER NOT NULL         -- 1 until we see the workflow is closed
);
"""


class QuotaExceeded(Exception):
    pass


class UnknownSession(Exception):
    """No such session for this tenant. Also used for another tenant's session: do not leak it."""


class NotResponder(Exception):
    """The caller is in the right tenant but is not the user who started the session."""


class SessionClosed(Exception):
    """The session has ended (finished, expired, stopped, timed out): "gone"."""


class NotOpenStep(Exception):
    """The session is not waiting at the step this reply is for."""


class Unavailable(Exception):
    """The session could not answer in time (for example no worker is running to answer a query)."""


@dataclass
class Quotas:
    default_max_active: int = 5
    per_tenant: dict[str, int] | None = None

    def limit(self, tenant: str) -> int:
        return (self.per_tenant or {}).get(tenant, self.default_max_active)


class SessionManager:
    def __init__(self, engine: SessionEngine, db_path: str, quotas: Quotas, agents_path: Path = DEFAULT_PATH):
        self._engine = engine
        self._db = db_path
        self._quotas = quotas
        self._agents_path = agents_path
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        conn.close()

    async def start(self, tenant: str, user: str, agent_id: str, version: int, message: str) -> str:
        agent = load_agent(agent_id, version, self._agents_path)  # pinned: copied into the input
        await self._close_finished(tenant)
        session_id = f"{tenant}-{uuid.uuid4().hex[:10]}"
        with txn(self._db) as c:  # check and insert in one transaction: no two starts both squeeze in
            active = c.execute("SELECT COUNT(*) FROM sessions WHERE tenant = ? AND active = 1", (tenant,)).fetchone()[0]
            if active >= self._quotas.limit(tenant):
                raise QuotaExceeded(f"tenant {tenant} has {active} active sessions (limit {self._quotas.limit(tenant)})")
            c.execute("INSERT INTO sessions VALUES (?, ?, ?, 1)", (session_id, tenant, user))
        try:
            await self._engine.start(session_id, tenant, agent, message)
        except BaseException:
            with txn(self._db) as c:
                c.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
            raise
        return session_id

    async def _close_finished(self, tenant: str) -> None:
        """Quota counts sessions that are still running. Ask the engine which ones are not.
        Bounded by the tenant's quota, since only active rows are looked at."""
        with txn(self._db) as c:
            ids = [r[0] for r in c.execute("SELECT session_id FROM sessions WHERE tenant = ? AND active = 1", (tenant,))]
        for session_id in ids:
            # NOT_FOUND: a row whose session never started (we died between insert and start).
            if await self._engine.status(session_id) != SessionStatus.RUNNING:
                with txn(self._db) as c:
                    c.execute("UPDATE sessions SET active = 0 WHERE session_id = ?", (session_id,))

    # ---- interaction with a running session ----

    async def reply(self, tenant: str, user: str, session_id: str, step: int, text: str) -> None:
        await self._authorize(tenant, user, session_id)
        try:
            open_step = await self._engine.open_step(session_id)
        except EngineUnavailable as e:
            raise Unavailable(str(e)) from e
        if open_step != step:
            raise NotOpenStep(f"session {session_id} is not waiting at step {step}")
        await self._engine.reply(session_id, step, text)
        # Between the check and the delivery the step can close. The session is the authority: it
        # drops a reply that is not for its open step, so a late or duplicate reply is harmless.

    async def message(self, tenant: str, user: str, session_id: str, message_id: str, text: str) -> None:
        await self._authorize(tenant, user, session_id)
        await self._engine.message(session_id, message_id, text)

    async def stop(self, tenant: str, user: str, session_id: str) -> None:
        await self._authorize(tenant, user, session_id)
        await self._engine.stop(session_id)

    async def _authorize(self, tenant: str, user: str, session_id: str) -> None:
        """Tenant check, then responder check, then 'is it still running'. Order matters: another
        tenant's session must look like it does not exist."""
        with txn(self._db) as c:
            row = c.execute("SELECT tenant, user FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        if row is None or row[0] != tenant:
            raise UnknownSession(session_id)
        if row[1] != user:
            raise NotResponder(session_id)
        if await self._engine.status(session_id) != SessionStatus.RUNNING:
            raise SessionClosed(session_id)
