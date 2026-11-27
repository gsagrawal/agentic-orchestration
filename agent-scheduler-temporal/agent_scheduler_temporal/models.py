"""Executor-side data: what the workflow and activities pass around (JSON-serialised by Temporal).

The session manager must not import this module; its types live in agent.py."""
from dataclasses import dataclass, field

from agent_scheduler_temporal.agent import AgentVersion

TASK_QUEUE = "agent-executor"

# How fast a dead worker is noticed. A tool call heartbeats every HEARTBEAT_INTERVAL_S; if Temporal
# sees none for HEARTBEAT_TIMEOUT_S it fails the attempt and retries it elsewhere. Without
# heartbeats the only signal would be the tool's whole start-to-close timeout (10-30 s here, and
# minutes for a real code tool). These values are ASSUMPTIONS chosen for tests, not tuned.
HEARTBEAT_INTERVAL_S = 1.0
HEARTBEAT_TIMEOUT_S = 3.0


@dataclass
class SessionState:
    """Everything the loop needs to carry into the next run after continue-as-new."""

    history: list[dict]
    step: int
    seen_message_ids: list[str]
    sandbox_created_before: bool
    sandbox_used: bool
    continued: int  # how many times this session has continued as new


@dataclass
class SessionInput:
    session_id: str
    tenant: str
    agent: AgentVersion
    message: str
    carry: SessionState | None = None  # None on the first run
    # Executor setting, set by the engine (not part of the agent): continue-as-new after this many
    # steps in one run; 0 = only when Temporal suggests it.
    continue_after_steps: int = 0


@dataclass
class SessionResult:
    status: str  # "finished", "step_cap", "stopped" or "expired"
    steps: int
    history: list[dict] = field(default_factory=list)
    continued: int = 0


@dataclass
class ModelRequest:
    session_id: str
    step: int
    history: list[dict]
    script: list[dict]
    loop: bool


@dataclass
class ToolCall:
    session_id: str
    step: int
    tool: str
    args: dict


@dataclass
class HumanRequest:
    session_id: str
    step: int
    question: str


@dataclass
class ReplySignal:
    step: int  # the step that asked; a reply for any other step is ignored
    text: str


@dataclass
class MessageSignal:
    message_id: str  # a repeated id is dropped
    text: str
