"""FAKED model (stands in for llm-gateway and the LLM).

A fixed script of actions, one per step. A real model reads the history; this one
only needs the step number, which keeps it a pure function of its input.
Actions: {"do": "tool", "tool": name, "args": {...}} | {"do": "ask_human", ...} | {"do": "finish"}
"""


def next_action(script: list[dict], loop: bool, step: int) -> dict:
    if loop:
        return script[step % len(script)]
    if step < len(script):
        return script[step]
    return {"do": "finish"}
