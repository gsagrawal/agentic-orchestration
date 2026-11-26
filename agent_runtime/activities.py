"""Activities: every call to the outside. The workflow decides; these do.

Component: agent-executor (the worker process hosts these next to the workflow).
Activities hold no secrets. The model, sandbox and tool service are injected.
"""
import asyncio

from temporalio import activity
from temporalio.exceptions import ApplicationError

from agent_runtime.models import ModelRequest, ToolCall
from agent_runtime.sandbox import FakeSandboxController
from agent_runtime.scripted_model import next_action
from agent_runtime.tool_service import StaleAttempt, ToolService


class Activities:
    def __init__(self, tool_service: ToolService, sandbox: FakeSandboxController):
        self._tools = tool_service
        self._sandbox = sandbox

    @activity.defn
    async def call_model(self, req: ModelRequest) -> dict:
        return next_action(req.script, req.loop, req.step)

    @activity.defn
    async def ensure_sandbox(self, session_id: str) -> bool:
        return self._sandbox.ensure(session_id)

    @activity.defn
    async def stop_sandbox(self, session_id: str) -> None:
        self._sandbox.stop(session_id)

    @activity.defn
    async def run_tool(self, call: ToolCall) -> dict:
        """At-least-once. The key is session id + step; the attempt is Temporal's attempt number,
        which rises on every retry, so the tool service can fence an older attempt."""
        key = f"{call.session_id}:{call.step}"
        attempt = activity.info().attempt
        try:
            # The tool service is blocking (SQLite, sleep): keep it off the event loop.
            result = await asyncio.to_thread(self._tools.execute, key, attempt, call.tool, call.args)
        except StaleAttempt as e:
            # A newer attempt owns this key. Retrying this one can never help.
            raise ApplicationError(str(e), type="StaleAttempt", non_retryable=True)
        return {"status": result.status, "value": result.value}

    @property
    def all(self):
        return [self.call_model, self.ensure_sandbox, self.stop_sandbox, self.run_tool]
