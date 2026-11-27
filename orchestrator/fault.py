"""Fault injection for tests: a hook that kills the process with SIGKILL at a named point.

The executor and the tool service call `fault_hook(point)` at the moments where a
crash is interesting. In production the hook is a no-op.

Points, in the order one tool call passes them:
    before_tool_call      executor: decision logged, tool service not yet called
    before_effect         tool service: about to run the tool (after its delay)
    after_effect          tool service, non-idempotent only: effect done, result not yet stored
    after_tool_call       executor: tool service returned, result not yet in the log
    after_result_logged   executor: result is in the log
"""

import os
import signal
from typing import Callable


def no_fault(point: str) -> None:
    """The production hook."""


def kill_self_at(target: str | None) -> Callable[[str], None]:
    def hook(point: str) -> None:
        if point == target:
            os.kill(os.getpid(), signal.SIGKILL)

    return hook
