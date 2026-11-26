"""Worker process (agent-executor): hosts the AgentSession workflow and its activities.

Run as its own OS process so that `kill -9` is real:
    python -m agent_runtime.worker --address localhost:7233 --data-dir /tmp/rt
The tool service and sandbox controller are FAKED and live in SQLite files under --data-dir,
shared by every worker process pointed at the same directory.
"""
import argparse
import asyncio
from datetime import timedelta
from pathlib import Path

from temporalio.client import Client
from temporalio.worker import Worker

from agent_runtime.activities import Activities
from agent_runtime.models import TASK_QUEUE
from agent_runtime.notifier import FakeNotifier
from agent_runtime.sandbox import FakeSandboxController
from agent_runtime.tool_service import ToolService
from agent_runtime.tools import FakeTools
from agent_runtime.workflow import AgentSession


# After a worker dies, the next workflow task for a session it was running is first offered to that
# worker's own ("sticky") queue and only falls back to the shared queue after this timeout. The SDK
# default is 10 s, which dominated recovery time in our first measurement. 2 s is an ASSUMPTION for
# tests; a shorter value makes a briefly slow worker lose its cached workflow sooner.
STICKY_TIMEOUT = timedelta(seconds=2)


def build_activities(data_dir: str, tool_delay_s: float = 0.0) -> Activities:
    root = Path(data_dir)
    root.mkdir(parents=True, exist_ok=True)
    return Activities(
        ToolService(str(root / "tools.db"), tools=FakeTools(delay_s=tool_delay_s)),
        FakeSandboxController(str(root / "sandbox.db")),
        FakeNotifier(str(root / "notifier.db")),
    )


async def run_worker(address: str, data_dir: str, tool_delay_s: float = 0.0, task_queue: str = TASK_QUEUE) -> None:
    client = await Client.connect(address)
    acts = build_activities(data_dir, tool_delay_s)
    async with Worker(
        client, task_queue=task_queue, workflows=[AgentSession], activities=acts.all,
        sticky_queue_schedule_to_start_timeout=STICKY_TIMEOUT,
    ):
        await asyncio.Event().wait()  # run until killed


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--address", default="localhost:7233")
    p.add_argument("--data-dir", required=True)
    p.add_argument("--tool-delay", type=float, default=0.0, help="seconds each fake tool sleeps (lets a kill land mid-call)")
    p.add_argument("--task-queue", default=TASK_QUEUE)
    args = p.parse_args()
    asyncio.run(run_worker(args.address, args.data_dir, args.tool_delay, args.task_queue))
