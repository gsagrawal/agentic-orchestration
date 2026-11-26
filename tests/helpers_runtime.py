import asyncio


async def wait_until(check, timeout=20.0, interval=0.05):
    """Polls an async or sync condition; fails the test on timeout instead of hanging."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        value = check()
        if asyncio.iscoroutine(value):
            value = await value
        if value:
            return value
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(interval)


import os
import subprocess
import sys
import uuid

from temporalio.api.enums.v1 import EventType
from temporalio.converter import DataConverter


class WorkerProcesses:
    """Spawns agent_runtime.worker as real OS processes on one task queue and data dir."""

    def __init__(self, address: str, data_dir: str):
        self.address = address
        self.data_dir = str(data_dir)
        self.task_queue = f"q-{uuid.uuid4().hex[:8]}"
        self.procs: list[subprocess.Popen] = []

    def spawn(self, tool_delay: float = 0.0, fault: str | None = None) -> subprocess.Popen:
        """fault="<point>:<kill|stop>" arms agent_runtime.fault in that process only."""
        env = dict(os.environ)
        if fault:
            env["AGENT_FAULT"] = fault
            env["AGENT_FAULT_FILE"] = os.path.join(self.data_dir, f"fault-{uuid.uuid4().hex[:6]}")
        proc = subprocess.Popen(
            [sys.executable, "-m", "agent_runtime.worker", "--address", self.address, "--data-dir", self.data_dir,
             "--task-queue", self.task_queue, "--tool-delay", str(tool_delay)],
            env=env,
        )
        proc.fault_file = env.get("AGENT_FAULT_FILE")
        self.procs.append(proc)
        return proc

    def cleanup(self) -> None:
        for p in self.procs:
            if p.poll() is None:
                p.kill()  # also ends a SIGSTOPped process
            p.wait()


async def model_inputs(handle) -> list[dict]:
    """What the model was shown, read from the workflow's recorded history (works across processes)."""
    inputs = []
    async for event in handle.fetch_history_events():
        if event.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED:
            attrs = event.activity_task_scheduled_event_attributes
            if attrs.activity_type.name == "call_model":
                [req] = await DataConverter.default.decode(attrs.input.payloads)
                inputs.append(req)
    return inputs
