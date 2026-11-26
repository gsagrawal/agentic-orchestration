"""Fault injection for tests: make THIS process die (SIGKILL) or freeze (SIGSTOP) at a named point.

Armed only when AGENT_FAULT="<point>:<kill|stop>" and AGENT_FAULT_FILE=<path> are set in the
process environment (tests set them on one worker process). It fires once: the first process to
create the file wins, so a replacement worker does not die at the same point.
Points, in the order they occur inside ToolService.execute:
  before_marker, before_effect, after_effect, after_result
"""
import os
import signal


def maybe_fault(point: str) -> None:
    spec = os.environ.get("AGENT_FAULT")
    if not spec:
        return
    want, action = spec.split(":")
    if want != point:
        return
    try:
        os.close(os.open(os.environ["AGENT_FAULT_FILE"], os.O_CREAT | os.O_EXCL | os.O_WRONLY))
    except FileExistsError:
        return
    os.kill(os.getpid(), signal.SIGKILL if action == "kill" else signal.SIGSTOP)
