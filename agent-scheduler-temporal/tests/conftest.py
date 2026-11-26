import pytest
from temporalio.testing import WorkflowEnvironment


@pytest.fixture
async def env():
    """Temporal test environment with time skipping.

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
    from agent_scheduler_temporal.harness import WorkerProcesses

    w = WorkerProcesses(dev_env.client.service_client.config.target_host, tmp_path)
    yield w
    w.cleanup()
