import pytest
from temporalio.testing import WorkflowEnvironment

from orchestrator import db


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "orch.db")
    db.init_db(path)
    return path


@pytest.fixture
def conn(db_path):
    c = db.connect(db_path)
    yield c
    c.close()


import json


@pytest.fixture
def agents_path(tmp_path):
    """FAKED agent store file. Version 1 of each agent; scripts are plain lists of actions."""

    def tool(name, **args):
        return {"do": "tool", "tool": name, "args": args}

    agents = {
        "two-tools": {"1": {"script": [tool("lookup", key="a"), tool("send_email", to="x"), {"do": "finish"}]}},
        "ask-human": {"1": {"script": [tool("lookup", key="a"), {"do": "wait"}, {"do": "finish"}]}},
        "set-value": {"1": {"script": [tool("set_value", key="k"), {"do": "finish"}]}},
        "send-email": {"1": {"script": [tool("send_email", to="x"), {"do": "finish"}]}},
        "poison": {"1": {"script": [tool("lookup", key="a")], "loop": True}},
    }
    path = tmp_path / "agents.json"
    path.write_text(json.dumps(agents))
    return str(path)


@pytest.fixture(scope="session")
async def env():
    """Temporal test environment with time skipping (agent_runtime tests)."""
    e = await WorkflowEnvironment.start_time_skipping()
    yield e
    await e.shutdown()
