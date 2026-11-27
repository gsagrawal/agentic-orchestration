"""The interface between the session manager and whatever runs sessions.

The manager (admission, quotas, who may talk to which session) depends only on this. Today the one
implementation is temporal_engine.TemporalEngine. To move to another engine (an API call to a
different runner, a message queue) write another class with these methods; the manager does not
change. The executor side (workflow, activities, worker) is not behind this interface: it IS the
Temporal-specific part.

Contract notes, so another implementation behaves the same way:
- start() hands over the agent version, which is pinned for the session. It must not return before
  the session is accepted, and it raises if it cannot be.
- status() never raises for an unknown session; it returns NOT_FOUND.
- open_step() asks the running session which step is waiting for a human (None: none). It must not
  wait forever: if the session cannot answer in time it raises EngineUnavailable.
- reply / message / stop are delivered at-least-once and may arrive late or twice. The session
  drops a reply that is not for its open step, a repeated message id, and acts on stop at its next
  step boundary. The engine does not have to filter.
"""
from enum import Enum
from typing import Protocol

from agent_scheduler_temporal.agent import AgentVersion


class SessionStatus(Enum):
    RUNNING = "running"  # started and not yet ended (including waiting for a human)
    ENDED = "ended"  # finished, expired, stopped, timed out, failed
    NOT_FOUND = "not_found"  # the engine has never heard of it


class EngineUnavailable(Exception):
    """The session could not be reached or did not answer in time."""


class SessionEngine(Protocol):
    async def start(self, session_id: str, tenant: str, agent: AgentVersion, message: str) -> None: ...

    async def status(self, session_id: str) -> SessionStatus: ...

    async def open_step(self, session_id: str) -> int | None: ...

    async def reply(self, session_id: str, step: int, text: str) -> None: ...

    async def message(self, session_id: str, message_id: str, text: str) -> None: ...

    async def stop(self, session_id: str) -> None: ...
