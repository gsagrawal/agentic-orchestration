"""FAKED: a few fake tools standing in for external APIs.

"Idempotent" here means the backend deduplicates on the idempotency key, so
the effect and the stored result are recorded together and a retry is always
safe. A non-idempotent tool has a window between the effect and the stored
result; a crash inside it leaves an effect with no recorded result.
`delay` (seconds) is spent before the effect so a kill can land mid-call.
"""

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Tool:
    name: str
    idempotent: bool
    run: Callable[[dict, int], dict]  # (args, effect_id) -> result
    delay: float = 0.0


def default_tools(delay: float = 0.0) -> dict[str, Tool]:
    tools = [
        # reads a value; safe to repeat
        Tool("lookup", True, lambda args, effect_id: {"value": f"value-of-{args['key']}"}, delay),
        # PUT-style: setting the same value twice leaves the same state
        Tool("set_value", True, lambda args, effect_id: {"set": args["key"]}, delay),
        # POST-style: every real run sends another email. The result carries the
        # effect id, so a second real run is visible as a different message_id.
        Tool("send_email", False, lambda args, effect_id: {"message_id": effect_id}, delay),
    ]
    return {t.name: t for t in tools}
