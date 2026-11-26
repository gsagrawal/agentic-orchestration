"""Step 3 integration test: Temporal dev server + a worker as a separate OS process."""
import subprocess
import sys

import pytest
from temporalio.testing import WorkflowEnvironment

from agent_runtime.agent_store import load_agent
from agent_runtime.models import TASK_QUEUE, SessionInput
from agent_runtime.tool_service import ToolService
from agent_runtime.workflow import AgentSession

pytestmark = pytest.mark.integration


async def test_one_session_runs_to_the_end_on_a_worker_process(tmp_path):
    async with await WorkflowEnvironment.start_local() as env:
        address = env.client.service_client.config.target_host
        worker = subprocess.Popen([sys.executable, "-m", "agent_runtime.worker", "--address", address, "--data-dir", str(tmp_path)])
        try:
            result = await env.client.execute_workflow(
                AgentSession.run,
                SessionInput("e2e-1", "tenant-1", load_agent("researcher", 1), "go"),
                id="e2e-1",
                task_queue=TASK_QUEUE,
            )
        finally:
            worker.kill()
            worker.wait()
    assert result.status == "finished"
    ledger = ToolService(str(tmp_path / "tools.db")).ledger
    assert [ledger.count(f"e2e-1:{i}") for i in range(4)] == [1, 1, 0, 1]  # step 2 (send_email) has no grant
    assert ledger.duplicates() == []
