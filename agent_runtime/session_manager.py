"""session-manager: admission, per-tenant quotas, and the start of a session workflow.

Admission is checked before the workflow is started. The manager keeps one small table
(session -> tenant, user, workflow state); it holds no call-by-call record.
"""
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path

from temporalio.client import Client, WorkflowExecutionStatus

from agent_runtime.agent_store import DEFAULT_PATH, load_agent
from agent_runtime.db import txn
from agent_runtime.models import TASK_QUEUE, MessageSignal, ReplySignal, SessionInput
from agent_runtime.workflow import AgentSession

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


@dataclass
class Quotas:
    default_max_active: int = 5
    per_tenant: dict[str, int] | None = None

    def limit(self, tenant: str) -> int:
        return (self.per_tenant or {}).get(tenant, self.default_max_active)


class SessionManager:
    def __init__(self, client: Client, db_path: str, quotas: Quotas, task_queue: str = TASK_QUEUE, agents_path: Path = DEFAULT_PATH):
        self._client = client
        self._db = db_path
        self._quotas = quotas
        self._task_queue = task_queue
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
            await self._client.start_workflow(
                AgentSession.run,
                SessionInput(session_id, tenant, agent, message),
                id=session_id,
                task_queue=self._task_queue,
                **self._timeouts(agent),
            )
        except BaseException:
            with txn(self._db) as c:
                c.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
            raise
        return session_id

    def _timeouts(self, agent) -> dict:
        return {}  # the active-run limit is added in step 7

    async def _close_finished(self, tenant: str) -> None:
        """Quota counts sessions that are still running. Ask Temporal which ones are not.
        Bounded by the tenant's quota, since only active rows are looked at."""
        with txn(self._db) as c:
            ids = [r[0] for r in c.execute("SELECT session_id FROM sessions WHERE tenant = ? AND active = 1", (tenant,))]
        for session_id in ids:
            desc = await self._client.get_workflow_handle(session_id).describe()
            if desc.status != WorkflowExecutionStatus.RUNNING:
                with txn(self._db) as c:
                    c.execute("UPDATE sessions SET active = 0 WHERE session_id = ?", (session_id,))

    # ---- interaction with a running session ----

    async def reply(self, tenant: str, user: str, session_id: str, step: int, text: str) -> None:
        handle = await self._open_handle(tenant, user, session_id)
        if await handle.query(AgentSession.open_step) != step:
            raise NotOpenStep(f"session {session_id} is not waiting at step {step}")
        await handle.signal(AgentSession.reply, ReplySignal(step, text))
        # Between the query and the signal the step can close. The workflow is the authority: it
        # drops a reply that is not for its open step, so a late or duplicate reply is harmless.

    async def message(self, tenant: str, user: str, session_id: str, message_id: str, text: str) -> None:
        handle = await self._open_handle(tenant, user, session_id)
        await handle.signal(AgentSession.message, MessageSignal(message_id, text))

    async def stop(self, tenant: str, user: str, session_id: str) -> None:
        handle = await self._open_handle(tenant, user, session_id)
        await handle.signal(AgentSession.stop)

    async def _open_handle(self, tenant: str, user: str, session_id: str):
        """Tenant check, then responder check, then 'is it still running'. Order matters: another
        tenant's session must look like it does not exist."""
        with txn(self._db) as c:
            row = c.execute("SELECT tenant, user FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        if row is None or row[0] != tenant:
            raise UnknownSession(session_id)
        if row[1] != user:
            raise NotResponder(session_id)
        handle = self._client.get_workflow_handle(session_id)
        if (await handle.describe()).status != WorkflowExecutionStatus.RUNNING:
            raise SessionClosed(session_id)
        return handle
