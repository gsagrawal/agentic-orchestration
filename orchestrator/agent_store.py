"""FAKED: stands in for agent-crud-service. Agent definitions live in a JSON file:

    {"<agent_id>": {"<version>": {"script": [...], "loop": false}}}

A session is pinned to one (agent_id, version) at admission, so editing the file
for a later version never changes a running session.
"""

import json

from .errors import UnknownAgent


class AgentStore:
    def __init__(self, path: str):
        with open(path) as f:
            self._agents = json.load(f)

    def latest_version(self, agent_id: str) -> int:
        if agent_id not in self._agents:
            raise UnknownAgent(agent_id)
        return max(int(v) for v in self._agents[agent_id])

    def get(self, agent_id: str, version: int) -> dict:
        return self._agents[agent_id][str(version)]
