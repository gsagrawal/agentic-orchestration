"""Spawns agent_scheduler_temporal.worker as real OS processes. Used by the integration tests, the chaos
test and the benchmark."""
import os
import subprocess
import sys
import uuid


class WorkerProcesses:
    """Spawns agent_scheduler_temporal.worker as real OS processes on one task queue and data dir."""

    def __init__(self, address: str, data_dir: str):
        self.address = address
        self.data_dir = str(data_dir)
        self.task_queue = f"q-{uuid.uuid4().hex[:8]}"
        self.procs: list[subprocess.Popen] = []

    def spawn(self, tool_delay: float = 0.0, fault: str | None = None) -> subprocess.Popen:
        """fault="<point>:<kill|stop>" arms agent_scheduler_temporal.fault in that process only."""
        env = dict(os.environ)
        if fault:
            env["AGENT_FAULT"] = fault
            env["AGENT_FAULT_FILE"] = os.path.join(self.data_dir, f"fault-{uuid.uuid4().hex[:6]}")
        proc = subprocess.Popen(
            [sys.executable, "-m", "agent_scheduler_temporal.worker", "--address", self.address, "--data-dir", self.data_dir,
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
