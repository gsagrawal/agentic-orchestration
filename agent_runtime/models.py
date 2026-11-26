"""Plain data passed between the workflow and the activities (JSON-serialised by Temporal)."""
from dataclasses import dataclass, field

from agent_runtime.tools import ToolSpec

TASK_QUEUE = "agent-executor"

# How fast a dead worker is noticed. A tool call heartbeats every HEARTBEAT_INTERVAL_S; if Temporal
# sees none for HEARTBEAT_TIMEOUT_S it fails the attempt and retries it elsewhere. Without
# heartbeats the only signal would be the tool's whole start-to-close timeout (10-30 s here, and
# minutes for a real code tool). These values are ASSUMPTIONS chosen for tests, not tuned.
HEARTBEAT_INTERVAL_S = 1.0
HEARTBEAT_TIMEOUT_S = 3.0


@dataclass
class AgentVersion:
    """Pinned for the whole session: it is workflow input, so later edits cannot reach it."""

    agent_id: str
    version: int
    tools: list[ToolSpec]  # the tools this version may use, with their metadata
    grants: list[str]  # a tool is allowed only if its spec.grant is in here
    step_cap: int
    script: list[dict]  # FAKED model: the steps it will take, see scripted_model.py
    loop: bool = False  # FAKED model: repeat the script forever (the "poison" agent)
    wait_expiry_s: float = 86400.0  # how long a session may wait for a human (the waiting timer)


@dataclass
class SessionInput:
    session_id: str
    tenant: str
    agent: AgentVersion
    message: str


@dataclass
class SessionResult:
    status: str  # "finished", "step_cap", "stopped" or "expired"
    steps: int
    history: list[dict] = field(default_factory=list)


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
