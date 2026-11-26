"""Plain data passed between the workflow and the activities (JSON-serialised by Temporal)."""
from dataclasses import dataclass, field

from agent_runtime.tools import ToolSpec

TASK_QUEUE = "agent-executor"


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


@dataclass
class SessionInput:
    session_id: str
    tenant: str
    agent: AgentVersion
    message: str


@dataclass
class SessionResult:
    status: str  # "finished" or "step_cap"
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
