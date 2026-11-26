"""FAKED agent-crud-service: agent versions from a JSON file.

The file lists tool names and grants; the metadata for each tool (type, idempotent flag,
timeout, grant) comes from FakeTools.SPECS, so adding a tool is a data change.
"""
import json
from pathlib import Path

from agent_runtime.models import AgentVersion
from agent_runtime.tools import FakeTools

DEFAULT_PATH = Path(__file__).with_name("agents.json")


def load_agent(agent_id: str, version: int, path: Path = DEFAULT_PATH) -> AgentVersion:
    data = json.loads(path.read_text())[agent_id][str(version)]
    return AgentVersion(
        agent_id=agent_id,
        version=version,
        tools=[FakeTools.SPECS[name] for name in data["tools"]],
        grants=data["grants"],
        step_cap=data["step_cap"],
        script=data["script"],
        loop=data.get("loop", False),
    )
