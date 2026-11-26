"""The AgentSession workflow: one generic loop for every agent.

It only decides. Every call to the outside is an activity. No wall-clock time, no
randomness and no I/O in here, so it replays deterministically.
Component: agent-executor.
"""
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from agent_runtime.activities import Activities
    from agent_runtime.models import ModelRequest, SessionInput, SessionResult, ToolCall

# Retries are unbounded by design (tool calls are at-least-once); only the backoff is capped.
RETRY = RetryPolicy(initial_interval=timedelta(seconds=1), maximum_interval=timedelta(seconds=10))
MODEL_TIMEOUT = timedelta(seconds=60)


@workflow.defn(name="AgentSession")
class AgentSession:
    @workflow.run
    async def run(self, inp: SessionInput) -> SessionResult:
        agent = inp.agent
        specs = {t.name: t for t in agent.tools}
        history: list[dict] = [{"role": "user", "content": inp.message}]
        sandbox_used = False
        step = 0
        status = "finished"
        try:
            while True:
                if step >= agent.step_cap:
                    status = "step_cap"
                    break
                action = await workflow.execute_activity_method(
                    Activities.call_model,
                    ModelRequest(inp.session_id, step, history, agent.script, agent.loop),
                    start_to_close_timeout=MODEL_TIMEOUT,
                    retry_policy=RETRY,
                )
                history.append({"role": "assistant", "step": step, "action": action})
                if action["do"] == "finish":
                    break
                if action["do"] != "tool":
                    raise ValueError(f"unsupported action {action['do']!r}")  # ask_human: step 5

                spec = specs.get(action["tool"])
                if spec is None or spec.grant not in agent.grants:
                    # Grant check is data-driven and needs no activity: the agent version is input.
                    history.append({"role": "tool", "step": step, "tool": action["tool"], "status": "denied"})
                else:
                    if spec.type == "code":
                        await workflow.execute_activity_method(
                            Activities.ensure_sandbox,
                            inp.session_id,
                            start_to_close_timeout=timedelta(seconds=30),
                            retry_policy=RETRY,
                        )
                        sandbox_used = True
                    result = await workflow.execute_activity_method(
                        Activities.run_tool,
                        ToolCall(inp.session_id, step, spec.name, action.get("args", {})),
                        start_to_close_timeout=timedelta(seconds=spec.timeout_s),
                        retry_policy=RETRY,
                    )
                    history.append({"role": "tool", "step": step, "tool": spec.name, **result})
                step += 1
        finally:
            if sandbox_used:
                await workflow.execute_activity_method(
                    Activities.stop_sandbox,
                    inp.session_id,
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=RETRY,
                )
        return SessionResult(status=status, steps=step, history=history)
