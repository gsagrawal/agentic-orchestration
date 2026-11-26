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


@pytest.fixture
async def env():
    """Temporal test environment with time skipping (agent_runtime tests).

    One server per test, on purpose: a session left running by one test (e.g. a quota test that
    starts sessions with no worker) would stall time skipping in every test after it."""
    e = await WorkflowEnvironment.start_time_skipping()
    yield e
    await e.shutdown()


@pytest.fixture(scope="session")
async def dev_env():
    """Temporal dev server (integration tests; separate OS processes connect to it)."""
    e = await WorkflowEnvironment.start_local()
    yield e
    await e.shutdown()


@pytest.fixture
def workers(dev_env, tmp_path):
    from tests.helpers_runtime import WorkerProcesses

    w = WorkerProcesses(dev_env.client.service_client.config.target_host, tmp_path)
    yield w
    w.cleanup()
