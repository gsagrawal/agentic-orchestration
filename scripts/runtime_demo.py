"""End-to-end demo: Temporal dev server + one worker process + one session run to the end.

    python3 -m scripts.runtime_demo
Starts the local dev server (downloaded by the temporalio SDK on first use), spawns a worker
as a separate OS process, runs the "researcher" agent, prints the result and the effects ledger.
"""
import asyncio
import subprocess
import sys
import tempfile

from temporalio.testing import WorkflowEnvironment

from agent_runtime.agent_store import load_agent
from agent_runtime.models import TASK_QUEUE, SessionInput
from agent_runtime.tool_service import ToolService
from agent_runtime.workflow import AgentSession


async def main() -> None:
    data_dir = tempfile.mkdtemp(prefix="rt-demo-")
    async with await WorkflowEnvironment.start_local() as env:
        address = env.client.service_client.config.target_host
        worker = subprocess.Popen([sys.executable, "-m", "agent_runtime.worker", "--address", address, "--data-dir", data_dir])
        try:
            result = await env.client.execute_workflow(
                AgentSession.run,
                SessionInput("demo-1", "tenant-1", load_agent("researcher", 1), "Check the weather and report."),
                id="demo-1",
                task_queue=TASK_QUEUE,
            )
        finally:
            worker.kill()
            worker.wait()
    print(f"status={result.status} steps={result.steps}")
    for h in result.history:
        print("  ", h)
    ledger = ToolService(f"{data_dir}/tools.db").ledger
    print("effects per step:", [ledger.count(f"demo-1:{i}") for i in range(result.steps)])
    print("duplicate effects:", ledger.duplicates())


if __name__ == "__main__":
    asyncio.run(main())
