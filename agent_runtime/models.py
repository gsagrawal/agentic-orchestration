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
    # The active-run limit: the manager sets it as the workflow EXECUTION timeout. Temporal counts
    # it as wall time from the start of the session, waiting included and across continue-as-new, so
    # it is really a lifetime limit; keep wait_expiry_s below it or the wait can never expire.
    active_limit_s: float = 3600.0
    continue_after_steps: int = 0  # continue-as-new after this many steps in one run; 0 = never


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
