"""SessionEngine on Temporal: the only place the session manager's side touches Temporal.

Session id = workflow id. The manager is a Temporal *client*: it starts workflows and signals,
queries and describes them. It never runs workflow code (that is the worker, worker.py).
"""
from datetime import timedelta

from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.service import RPCError, RPCStatusCode

from agent_scheduler_temporal.engine import EngineUnavailable, SessionStatus
from agent_scheduler_temporal.models import TASK_QUEUE, AgentVersion, MessageSignal, ReplySignal, SessionInput
from agent_scheduler_temporal.workflow import AgentSession


class TemporalEngine:
    def __init__(self, client: Client, task_queue: str = TASK_QUEUE, query_timeout_s: float = 5.0):
        self._client = client
        self._task_queue = task_queue
        self._query_timeout = timedelta(seconds=query_timeout_s)

    async def start(self, session_id: str, tenant: str, agent: AgentVersion, message: str) -> None:
        await self._client.start_workflow(
            AgentSession.run,
            SessionInput(session_id, tenant, agent, message),
            id=session_id,
            task_queue=self._task_queue,
            # The active-run limit is Temporal's workflow execution timeout (see models.AgentVersion).
            execution_timeout=timedelta(seconds=agent.active_limit_s),
        )

    async def status(self, session_id: str) -> SessionStatus:
        try:
            desc = await self._client.get_workflow_handle(session_id).describe()
        except RPCError as e:
            if e.status == RPCStatusCode.NOT_FOUND:
                return SessionStatus.NOT_FOUND
            raise
        return SessionStatus.RUNNING if desc.status == WorkflowExecutionStatus.RUNNING else SessionStatus.ENDED

    async def open_step(self, session_id: str) -> int | None:
        # A query is answered by a worker, so with none running it would wait: bound it.
        try:
            return await self._client.get_workflow_handle(session_id).query(
                AgentSession.open_step, rpc_timeout=self._query_timeout
            )
        except RPCError as e:
            raise EngineUnavailable(f"session {session_id} did not answer: {e.message}") from e

    async def reply(self, session_id: str, step: int, text: str) -> None:
        await self._client.get_workflow_handle(session_id).signal(AgentSession.reply, ReplySignal(step, text))

    async def message(self, session_id: str, message_id: str, text: str) -> None:
        await self._client.get_workflow_handle(session_id).signal(AgentSession.message, MessageSignal(message_id, text))

    async def stop(self, session_id: str) -> None:
        await self._client.get_workflow_handle(session_id).signal(AgentSession.stop)
