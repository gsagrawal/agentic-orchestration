"""The AgentSession workflow: one generic loop for every agent.

It only decides. Every call to the outside is an activity. No wall-clock time, no
randomness and no I/O in here, so it replays deterministically.
Component: agent-executor.

Step boundary = the top of the loop, between two steps. It is the only place where
queued messages are added to the history and where a stop request is acted on, so a
tool call that is in flight is never interrupted.
"""
import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from agent_scheduler_temporal.activities import Activities
    from agent_scheduler_temporal.models import (
        HEARTBEAT_TIMEOUT_S, HumanRequest, MessageSignal, ModelRequest, ReplySignal, SessionInput, SessionResult,
        SessionState, ToolCall,
    )

# Retries are unbounded by design (tool calls are at-least-once); only the backoff is capped.
RETRY = RetryPolicy(initial_interval=timedelta(seconds=1), maximum_interval=timedelta(seconds=10))
MODEL_TIMEOUT = timedelta(seconds=60)
SHORT_TIMEOUT = timedelta(seconds=30)
SANDBOX_RECREATED = "The sandbox was recreated. Files created in earlier steps are gone."


@workflow.defn(name="AgentSession")
class AgentSession:
    def __init__(self) -> None:
        self._inbox: list[dict] = []  # messages not yet shown to the model
        self._seen_message_ids: set[str] = set()
        self._replies: dict[int, str] = {}  # step -> the first reply received for it
        self._open_step: int | None = None  # the step currently waiting for a human
        self._stop_requested = False

    # ---- signals: handlers only record; the loop acts on them at the boundary ----

    @workflow.signal
    def reply(self, r: ReplySignal) -> None:
        # Only the open step takes a reply; a reply for another step or a repeat is ignored.
        if r.step == self._open_step and r.step not in self._replies:
            self._replies[r.step] = r.text

    @workflow.signal
    def message(self, m: MessageSignal) -> None:
        if m.message_id in self._seen_message_ids:
            return
        self._seen_message_ids.add(m.message_id)
        self._inbox.append({"role": "user", "message_id": m.message_id, "content": m.text})

    @workflow.signal
    def stop(self) -> None:
        self._stop_requested = True

    @workflow.query
    def open_step(self) -> int | None:
        return self._open_step

    # ---- the loop ----

    @workflow.run
    async def run(self, inp: SessionInput) -> SessionResult:
        agent = inp.agent
        specs = {t.name: t for t in agent.tools}
        carry = inp.carry or SessionState([{"role": "user", "content": inp.message}], 0, [], False, False, 0)
        history = carry.history
        self._seen_message_ids = set(carry.seen_message_ids)
        sandbox_used = carry.sandbox_used
        sandbox_created_before = carry.sandbox_created_before
        step = carry.step
        first_step_of_run = step
        status = "finished"
        continuing = False
        try:
            while True:
                history.extend(self._inbox)  # step boundary: messages become visible to the model
                self._inbox.clear()
                if self._stop_requested:
                    status = "stopped"
                    break
                if step >= agent.step_cap:
                    status = "step_cap"
                    break
                if agent.continue_after_steps and step - first_step_of_run >= agent.continue_after_steps:
                    # Long history: start a fresh run, carrying the history forward. The sandbox is kept.
                    continuing = True
                    workflow.continue_as_new(SessionInput(
                        inp.session_id, inp.tenant, agent, inp.message,
                        SessionState(history, step, sorted(self._seen_message_ids), sandbox_created_before,
                                     sandbox_used, carry.continued + 1),
                    ))
                action = await workflow.execute_activity_method(
                    Activities.call_model,
                    ModelRequest(inp.session_id, step, history, agent.script, agent.loop),
                    start_to_close_timeout=MODEL_TIMEOUT,
                    retry_policy=RETRY,
                )
                history.append({"role": "assistant", "step": step, "action": action})
                if action["do"] == "finish":
                    break
                if action["do"] == "ask_human":
                    answered = await self._ask_human(inp.session_id, step, action["question"], agent.wait_expiry_s, history)
                    if answered:
                        step += 1
                        continue
                    if not self._stop_requested:
                        status = "expired"
                        break
                    continue  # stopped while waiting: the boundary above ends the session
                if action["do"] != "tool":
                    # A plain exception would make Temporal retry the task forever; this fails the session.
                    raise ApplicationError(f"unsupported action {action['do']!r}", non_retryable=True)

                spec = specs.get(action["tool"])
                if spec is None or spec.grant not in agent.grants:
                    # Grant check is data-driven and needs no activity: the agent version is input.
                    history.append({"role": "tool", "step": step, "tool": action["tool"], "status": "denied"})
                else:
                    if spec.type == "code":
                        created = await workflow.execute_activity_method(
                            Activities.ensure_sandbox, inp.session_id,
                            start_to_close_timeout=SHORT_TIMEOUT, retry_policy=RETRY,
                        )
                        if created and sandbox_created_before:
                            # We had one and it is gone: tell the model, so it does not assume its files exist.
                            history.append({"role": "system", "step": step, "content": SANDBOX_RECREATED})
                        sandbox_used = sandbox_created_before = True
                    result = await workflow.execute_activity_method(
                        Activities.run_tool,
                        ToolCall(inp.session_id, step, spec.name, action.get("args", {})),
                        start_to_close_timeout=timedelta(seconds=spec.timeout_s),
                        heartbeat_timeout=timedelta(seconds=HEARTBEAT_TIMEOUT_S),
                        retry_policy=RETRY,
                    )
                    history.append({"role": "tool", "step": step, "tool": spec.name, **result})
                step += 1
        finally:
            if sandbox_used and not continuing:
                await workflow.execute_activity_method(
                    Activities.stop_sandbox, inp.session_id,
                    start_to_close_timeout=SHORT_TIMEOUT, retry_policy=RETRY,
                )
        return SessionResult(status=status, steps=step, history=history, continued=carry.continued)

    async def _ask_human(self, session_id: str, step: int, question: str, wait_expiry_s: float, history: list[dict]) -> bool:
        """Notify once, then wait on a signal with no worker held. True if a reply arrived."""
        self._open_step = step
        await workflow.execute_activity_method(
            Activities.request_human, HumanRequest(session_id, step, question),
            start_to_close_timeout=SHORT_TIMEOUT, retry_policy=RETRY,
        )
        try:
            await workflow.wait_condition(
                lambda: step in self._replies or self._stop_requested,
                timeout=timedelta(seconds=wait_expiry_s),
            )
        except asyncio.TimeoutError:
            await workflow.execute_activity_method(
                Activities.mark_expired, session_id,
                start_to_close_timeout=SHORT_TIMEOUT, retry_policy=RETRY,
            )
            self._open_step = None
            return False
        self._open_step = None
        if step not in self._replies:
            return False
        history.append({"role": "human", "step": step, "content": self._replies[step]})
        return True
