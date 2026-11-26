"""Activities: every call to the outside. The workflow decides; these do.

Component: agent-executor (the worker process hosts these next to the workflow).
Activities hold no secrets. The model, sandbox and tool service are injected.
"""
import asyncio

from temporalio import activity
from temporalio.exceptions import ApplicationError

from agent_scheduler_temporal.models import HEARTBEAT_INTERVAL_S, HumanRequest, ModelRequest, ToolCall
from agent_scheduler_temporal.notifier import FakeNotifier
from agent_scheduler_temporal.sandbox import FakeSandboxController
from agent_scheduler_temporal.scripted_model import ScriptedModel
from agent_scheduler_temporal.tool_service import StaleAttempt, ToolService


class Activities:
    def __init__(
        self,
        tool_service: ToolService,
        sandbox: FakeSandboxController,
        notifier: FakeNotifier | None = None,
        model: ScriptedModel | None = None,
    ):
        self._tools = tool_service
        self._sandbox = sandbox
        self._notifier = notifier or FakeNotifier(tool_service.path + ".notifier")
        self._model = model or ScriptedModel()

    @activity.defn
    async def call_model(self, req: ModelRequest) -> dict:
        return self._model.call(req)

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
            # The tool service is blocking (SQLite, sleep): keep it off the event loop, and
            # heartbeat from the loop while it runs so Temporal can tell this worker is alive.
            running = asyncio.ensure_future(asyncio.to_thread(self._tools.execute, key, attempt, call.tool, call.args))
            while True:
                done, _ = await asyncio.wait({running}, timeout=HEARTBEAT_INTERVAL_S)
                if done:
                    result = running.result()
                    break
                activity.heartbeat()
        except StaleAttempt as e:
            # A newer attempt owns this key. Retrying this one can never help.
            raise ApplicationError(str(e), type="StaleAttempt", non_retryable=True)
        return {"status": result.status, "value": result.value}

    @activity.defn
    async def request_human(self, req: HumanRequest) -> bool:
        """Sends the notification once per (session, step); a retry finds the key and sends nothing."""
        return self._notifier.notify(f"{req.session_id}:{req.step}", req.question)

    @activity.defn
    async def mark_expired(self, session_id: str) -> None:
        self._notifier.expire(session_id)

    @property
    def all(self):
        return [
            self.call_model, self.ensure_sandbox, self.stop_sandbox, self.run_tool,
            self.request_human, self.mark_expired,
        ]
