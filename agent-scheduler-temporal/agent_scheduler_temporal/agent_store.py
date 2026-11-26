"""FAKED agent-crud-service: agent versions from a JSON file.

The file lists tool names and grants; the metadata for each tool (type, idempotent flag,
timeout, grant) comes from FakeTools.SPECS, so adding a tool is a data change.
"""
import json
from pathlib import Path

from agent_scheduler_temporal.models import AgentVersion
from agent_scheduler_temporal.tools import FakeTools

DEFAULT_PATH = Path(__file__).with_name("agents.json")


class UnknownAgent(Exception):
    """No such agent or version in the store."""


def load_agent(agent_id: str, version: int, path: Path = DEFAULT_PATH) -> AgentVersion:
    try:
        data = json.loads(path.read_text())[agent_id][str(version)]
    except KeyError:
        raise UnknownAgent(f"{agent_id} version {version}") from None
    return AgentVersion(
        agent_id=agent_id,
        version=version,
        tools=[FakeTools.SPECS[name] for name in data["tools"]],
        grants=data["grants"],
        step_cap=data["step_cap"],
        script=data["script"],
        loop=data.get("loop", False),
        wait_expiry_s=data.get("wait_expiry_s", 86400.0),
        active_limit_s=data.get("active_limit_s", 3600.0),
        continue_after_steps=data.get("continue_after_steps", 0),
    )
