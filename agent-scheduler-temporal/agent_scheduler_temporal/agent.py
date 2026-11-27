"""The agent definition and tool metadata: what the session manager knows about an agent.

Manager-facing and engine-neutral: nothing here mentions workflows, activities, signals or
Temporal. The session manager loads an AgentVersion, checks admission against it, and hands it
to the engine, which pins it for the session. How an engine runs the session (for example
continue-as-new thresholds) is the engine's own setting, not part of the agent.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class ToolSpec:
    name: str
    type: str  # "api" or "code"
    idempotent: bool  # True: the backend dedupes on the key, so re-running is safe
    timeout_s: float
    grant: str  # the permission an agent version must hold to call this tool


@dataclass
class AgentVersion:
    """Pinned for the whole session: the engine receives a copy at start, so later edits cannot reach it."""

    agent_id: str
    version: int
    tools: list[ToolSpec]  # the tools this version may use, with their metadata
    grants: list[str]  # a tool is allowed only if its spec.grant is in here
    # Limits set at admission (the architecture doc has the manager set the deadline and step cap).
    step_cap: int
    # The agent's model definition. FAKED: a script of steps (see scripted_model.py) and whether it
    # repeats forever (the "poison" agent). A real definition holds the model name and instructions.
    script: list[dict]
    loop: bool = False
    wait_expiry_s: float = 86400.0  # how long a session may wait for a human (the waiting timer)
    # The active-run limit. How an engine enforces it is its own business; Temporal uses the
    # workflow execution timeout, which counts from the start of the session, waiting included, so
    # it is really a lifetime limit: keep wait_expiry_s below it or the wait can never expire.
    active_limit_s: float = 3600.0
