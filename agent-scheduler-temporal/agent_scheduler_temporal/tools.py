"""The FAKED tools and their metadata (ToolSpec, defined in agent.py).

Tool metadata is data, not code: adding a tool means adding a ToolSpec; the loop and the tool
service never branch on a tool's name. Component: tool service.
"""
import time

from agent_scheduler_temporal.agent import ToolSpec


class FakeTools:
    """FAKED tools. `delay_s` is slept *before* the effect so a test can land a
    kill mid-call; it is slept in the tool service, never in workflow code."""

    SPECS = {
        "lookup": ToolSpec("lookup", "api", idempotent=True, timeout_s=10, grant="read"),
        "set_value": ToolSpec("set_value", "api", idempotent=True, timeout_s=10, grant="write"),
        "send_email": ToolSpec("send_email", "api", idempotent=False, timeout_s=10, grant="email"),
        "run_code": ToolSpec("run_code", "code", idempotent=False, timeout_s=30, grant="code"),
    }

    def __init__(self, delay_s: float = 0.0):
        self.delay_s = delay_s

    def run(self, ledger, spec: ToolSpec, key: str, attempt: int, args: dict) -> dict:
        time.sleep(self.delay_s)
        if spec.idempotent:
            # Models a backend that dedupes on the idempotency key.
            ledger.record_once(key, attempt, spec.name)
        else:
            ledger.record(key, attempt, spec.name)
        return {"tool": spec.name, "ok": True, "args": args}
