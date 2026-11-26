"""agent-executor (stub): runs the loop. Holds no secrets and no cluster permissions.

The real executor calls a model to decide each step. Here the "model" is a
scripted list of tool calls, which is the only part faked. Everything around
it is the real shape: check the call against the agent's grants, the session's
step cap and the session's deadline, create the sandbox lazily on the first call
that needs one, send the code to it, append to the session log, return the result.
"""
from __future__ import annotations

import dataclasses
import json
import math
import time
from dataclasses import dataclass
from pathlib import Path

from agent_network_proxy.registry import Caller
from sandbox_controller import Limits, SandboxController, SandboxGone

INTERPRETERS = {"bash": ["bash", "-c"], "python": ["python3", "-c"]}


@dataclass(frozen=True)
class ToolCall:
    tool: str
    code: str


class AgentExecutor:
    def __init__(self, controller: SandboxController, grants_path: Path, session_log: Path,
                 step_cap: int = 10, timeout_s: float = 30, deadline_s: float = 600,
                 limits: Limits = Limits()):
        self.controller, self.grants_path, self.session_log = controller, grants_path, session_log
        self.step_cap, self.timeout_s, self.deadline_s, self.limits = step_cap, timeout_s, deadline_s, limits

    def run(self, caller: Caller, script: list[ToolCall], workdir: Path) -> list[dict]:
        agent = json.loads(self.grants_path.read_text())["agents"][caller.agent]
        if agent["tenant"] != caller.tenant:
            raise ValueError(f"agent {caller.agent} belongs to {agent['tenant']}, not {caller.tenant}")
        session_end = time.monotonic() + self.deadline_s       # the session's deadline bounds everything below
        sandbox, results = None, []
        try:
            for step, call in enumerate(script, start=1):
                remaining = session_end - time.monotonic()
                if step > self.step_cap:
                    results.append(self._log(caller, step, call, {"error": "step cap reached"}))
                    break
                if remaining <= 0:
                    results.append(self._log(caller, step, call, {"error": "session deadline reached"}))
                    break
                if call.tool not in agent["tools"]:
                    results.append(self._log(caller, step, call, {"error": f"tool '{call.tool}' not granted"}))
                    continue
                sandbox, result = self._run_step(sandbox, caller, call, workdir, session_end)
                results.append(self._log(caller, step, call, result))
        finally:
            if sandbox is not None and sandbox.alive:
                self.controller.stop(sandbox)
        return results

    def _run_step(self, sandbox, caller, call, workdir, session_end):
        """Run one call. A sandbox that has vanished (so the command never started) is replaced, once."""
        for attempt in (1, 2):
            remaining = session_end - time.monotonic()
            if attempt == 2 and remaining <= 0:       # the first attempt was checked by run(); a replacement must be too
                return sandbox, {"error": "session deadline reached"}
            if sandbox is None or not sandbox.alive:             # lazy: only a call that needs code pays for a sandbox
                lifetime = max(1, math.ceil(min(self.limits.deadline_s, remaining)))
                sandbox = self.controller.create(workdir, caller, dataclasses.replace(self.limits, deadline_s=lifetime))
            try:
                r = self.controller.exec(sandbox, [*INTERPRETERS[call.tool], call.code], min(self.timeout_s, remaining))
            except SandboxGone:
                sandbox = None
                continue
            return sandbox, {"exit_code": r.exit_code, "stdout": r.stdout, "stderr": r.stderr,
                             "timed_out": r.timed_out, "duration_s": round(r.duration_s, 3)}
        return sandbox, {"error": "sandbox kept disappearing"}

    def _log(self, caller: Caller, step: int, call: ToolCall, result: dict) -> dict:
        record = {"ts": time.time(), "session": caller.session, "agent": caller.agent,
                  "tenant": caller.tenant, "epoch": caller.epoch, "step": step,
                  "tool": call.tool, "code": call.code, "result": result}
        with self.session_log.open("a") as f:
            f.write(json.dumps(record) + "\n")
        return record
