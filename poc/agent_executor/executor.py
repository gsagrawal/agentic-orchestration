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
from sandbox_controller import Limits, Sandbox, SandboxController, SandboxError, SandboxGone

INTERPRETERS = {"bash": ["bash", "-c"], "python": ["python3", "-c"]}


@dataclass
class _Session:
    sandboxes: list[Sandbox] = dataclasses.field(default_factory=list)   # every sandbox this session created

    @property
    def sandbox(self) -> Sandbox | None:
        return self.sandboxes[-1] if self.sandboxes else None


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
        agent = json.loads(self.grants_path.read_text())["agents"].get(caller.agent)
        if agent is None:
            raise ValueError(f"unknown agent {caller.agent}")
        if agent["tenant"] != caller.tenant:
            raise ValueError(f"agent {caller.agent} belongs to {agent['tenant']}, not {caller.tenant}")
        if unknown := set(agent["tools"]) - set(INTERPRETERS):
            raise ValueError(f"agent {caller.agent} is granted tools this executor cannot run: {sorted(unknown)}")
        session_end = time.monotonic() + self.deadline_s       # the session's deadline bounds everything below
        session = _Session()
        results = []
        try:
            for step, call in enumerate(script, start=1):
                if step > self.step_cap:
                    results.append(self._log(caller, step, call, {"error": "step cap reached"}))
                    break
                if time.monotonic() >= session_end:
                    results.append(self._log(caller, step, call, {"error": "session deadline reached"}))
                    break
                if call.tool not in agent["tools"]:
                    results.append(self._log(caller, step, call, {"error": f"tool '{call.tool}' not granted"}))
                    continue
                try:
                    result = self._run_step(session, caller, call, workdir, session_end)
                except (SandboxError, OSError) as exc:      # infrastructure failure: report it, keep the results so far
                    result = {"error": f"{type(exc).__name__}: {exc}"}
                results.append(self._log(caller, step, call, result))
        finally:
            for sandbox in session.sandboxes:           # every one, not just the latest: an earlier stop may have half-failed
                if not sandbox.stopped:
                    try:
                        self.controller.stop(sandbox)
                    except (SandboxError, OSError) as exc:        # do not throw away the work that was done
                        results.append(self._log(caller, len(results) + 1, ToolCall("(cleanup)", ""),
                                                 {"error": f"cleanup failed: {type(exc).__name__}: {exc}"}))
        return results

    def _run_step(self, session: "_Session", caller: Caller, call: ToolCall, workdir: Path, session_end: float) -> dict:
        """Run one call. A sandbox that has vanished (so the command never started) is replaced, once.
        The sandbox is recorded on `session` the moment it exists, so run() can always clean it up."""
        for attempt in (1, 2):
            if session.sandbox is None or not session.sandbox.alive:    # lazy: only a call that needs code pays for one
                lifetime = max(1, math.ceil(min(self.limits.deadline_s, session_end - time.monotonic())))
                session.sandboxes.append(self.controller.create(workdir, caller, dataclasses.replace(self.limits, deadline_s=lifetime)))
            remaining = session_end - time.monotonic()                 # after creation: creating a sandbox takes time
            if remaining <= 0:
                return {"error": "session deadline reached"}
            try:
                r = self.controller.exec(session.sandbox, [*INTERPRETERS[call.tool], call.code], min(self.timeout_s, remaining))
            except SandboxGone:
                continue
            return {"exit_code": r.exit_code, "stdout": r.stdout, "stderr": r.stderr,
                    "timed_out": r.timed_out, "truncated": r.truncated, "duration_s": round(r.duration_s, 3)}
        return {"error": "sandbox kept disappearing"}

    def _log(self, caller: Caller, step: int, call: ToolCall, result: dict) -> dict:
        record = {"ts": time.time(), "session": caller.session, "agent": caller.agent,
                  "tenant": caller.tenant, "epoch": caller.epoch, "step": step,
                  "tool": call.tool, "code": call.code, "result": result}
        with self.session_log.open("a") as f:
            f.write(json.dumps(record) + "\n")
        return record
