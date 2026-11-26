"""Measurements for the Temporal runtime. Prints markdown; every number comes from this run.

    python3 -m scripts.bench [--sessions 60] [--runs 3] [--trials 20]

1. Throughput: N trivial sessions (3 tool calls each, no tool delay), sessions per second from the
   first start to the last result, at 1, 2 and 4 worker processes.
2. Death to resume: a worker is SIGKILLed while it runs a tool call; time from the kill until the
   tool service sees the next attempt begin (and until the session finishes), on a surviving worker.

Everything (dev server, workers, this client) shares one machine, so the numbers describe this
machine and these settings, not a deployment.
"""
import argparse
import asyncio
import os
import platform
import statistics
import tempfile
import time

from temporalio.testing import WorkflowEnvironment

from agent_scheduler_temporal.agent_store import load_agent
from agent_scheduler_temporal.harness import WorkerProcesses
from agent_scheduler_temporal.models import HEARTBEAT_INTERVAL_S, HEARTBEAT_TIMEOUT_S, AgentVersion, SessionInput
from agent_scheduler_temporal.tool_service import ToolService
from agent_scheduler_temporal.tools import FakeTools
from agent_scheduler_temporal.worker import STICKY_TIMEOUT
from agent_scheduler_temporal.workflow import AgentSession


def trivial_agent() -> AgentVersion:
    script = [{"do": "tool", "tool": "lookup", "args": {}}] * 3 + [{"do": "finish"}]
    return AgentVersion("bench", 1, list(FakeTools.SPECS.values()), ["read"], 20, script)


async def throughput(env, address, n_sessions: int, n_workers: int) -> float:
    workers = WorkerProcesses(address, tempfile.mkdtemp())
    try:
        for _ in range(n_workers):
            workers.spawn()
        await asyncio.sleep(4)  # let the processes import and start polling; not part of the measurement
        start = time.monotonic()
        handles = [
            await env.client.start_workflow(
                AgentSession.run, SessionInput(f"{workers.task_queue}-b-{i}", "t", trivial_agent(), "go"),
                id=f"{workers.task_queue}-b-{i}", task_queue=workers.task_queue,
            )
            for i in range(n_sessions)
        ]
        await asyncio.gather(*(h.result() for h in handles))
        return n_sessions / (time.monotonic() - start)
    finally:
        workers.cleanup()


async def death_to_resume(env, address, trials: int) -> tuple[list[float], list[float]]:
    workers = WorkerProcesses(address, tempfile.mkdtemp())
    tools = ToolService(f"{workers.data_dir}/tools.db")
    procs = {p.pid: p for p in (workers.spawn(tool_delay=2.0), workers.spawn(tool_delay=2.0))}
    await asyncio.sleep(4)
    resume, finish = [], []
    try:
        for i in range(trials):
            sid = f"{workers.task_queue}-d-{i}"  # session id and workflow id must be the same string
            handle = await env.client.start_workflow(
                AgentSession.run, SessionInput(sid, "t", load_agent("setter", 1), "go"), id=sid, task_queue=workers.task_queue,
            )
            key = f"{sid}:0"
            while tools.state(key) != "started":  # the tool call is running on some worker
                await asyncio.sleep(0.02)
            pid = tools.attempts_with_pid(key)[0][2]
            procs.pop(pid).kill()  # SIGKILL the worker that holds the call
            killed_at = time.time()
            new = workers.spawn(tool_delay=2.0)  # keep two workers
            procs[new.pid] = new
            await handle.result()
            done_at = time.time()
            second = [at for a, at in tools.attempts(key) if a >= 2][0]
            resume.append(second - killed_at)
            finish.append(done_at - killed_at)
        return resume, finish
    finally:
        workers.cleanup()


def pct(values: list[float], q: int) -> float:
    return statistics.quantiles(values, n=100, method="inclusive")[q - 1]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions", type=int, default=60)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--trials", type=int, default=20)
    args = ap.parse_args()
    cpu = next((l.split(":")[1].strip() for l in open("/proc/cpuinfo") if l.startswith("model name")), platform.processor())
    print(f"Machine: {os.cpu_count()} vCPU, {cpu}; Python {platform.python_version()}; dev server in-memory, one node.\n")
    async with await WorkflowEnvironment.start_local() as env:
        address = env.client.service_client.config.target_host
        print(f"## Throughput ({args.sessions} sessions x 3 tool calls, tool delay 0, {args.runs} runs each)\n")
        print("| workers | sessions/s per run | median |\n|---|---|---|")
        for n_workers in (1, 2, 4):
            runs = [await throughput(env, address, args.sessions, n_workers) for _ in range(args.runs)]
            print(f"| {n_workers} | {', '.join(f'{r:.1f}' for r in runs)} | {statistics.median(runs):.1f} |", flush=True)
        resume, finish = await death_to_resume(env, address, args.trials)
        print(f"\n## Worker death to resume ({args.trials} trials, 2 workers, SIGKILL mid tool call)\n")
        print(f"Settings: heartbeat interval {HEARTBEAT_INTERVAL_S} s, heartbeat timeout {HEARTBEAT_TIMEOUT_S} s, "
              f"sticky queue timeout {STICKY_TIMEOUT.total_seconds():.0f} s, activity retry backoff starts at 1 s, tool delay 2 s.\n")
        print("| measure | p50 | p95 | min | max |\n|---|---|---|---|---|")
        for name, v in (("kill -> next attempt starts (s)", resume), ("kill -> session finished (s)", finish)):
            print(f"| {name} | {pct(v, 50):.2f} | {pct(v, 95):.2f} | {min(v):.2f} | {max(v):.2f} |")
        print(f"\nn = {len(resume)} per row; with this n the p95 is set by the two or three slowest trials.")


if __name__ == "__main__":
    asyncio.run(main())
