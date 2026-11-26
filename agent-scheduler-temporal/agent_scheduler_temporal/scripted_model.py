"""FAKED model (stands in for llm-gateway and the LLM).

A fixed script of actions, one per step. A real model reads the history; this one only needs
the step number, which keeps it a pure function of its input. With record=True it also keeps what
it was shown, in memory, so an in-process test can check what the model saw (worker processes
cannot share that list; their tests read the workflow history instead). Off by default: a
long-lived worker would otherwise keep every request forever.
Actions: {"do": "tool", "tool": name, "args": {...}} | {"do": "ask_human", "question": str} | {"do": "finish"}
"""
from agent_scheduler_temporal.models import ModelRequest


def next_action(script: list[dict], loop: bool, step: int) -> dict:
    if loop:
        return script[step % len(script)]
    if step < len(script):
        return script[step]
    return {"do": "finish"}


class ScriptedModel:
    def __init__(self, record: bool = False):
        self._record = record
        self.seen: list[ModelRequest] = []

    def call(self, req: ModelRequest) -> dict:
        if self._record:
            self.seen.append(req)
        return next_action(req.script, req.loop, req.step)
