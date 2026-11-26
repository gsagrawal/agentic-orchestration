"""make bench (python3 -m scripts.bench): measure throughput and the time from executor death to a resumed session.

Prints the machine and every setting, because the numbers mean nothing without them.
Each number is measured on this machine in this run; nothing is estimated.
"""

import math
import os
import platform
import random
import sqlite3
import statistics
import tempfile
import time
from pathlib import Path

from orchestrator.cluster import Cluster
from orchestrator.session_manager import ManagerConfig

AGENTS = str(Path(__file__).parent / "agents.json")
THROUGHPUT_DELAYS = (0.01, 0.0)  # seconds per fake tool call (spent again on the way back for send_email)
SESSIONS = 200
REPEATS = 3
EXECUTOR_COUNTS = (1, 2, 4, 8)


def machine() -> str:
    with open("/proc/cpuinfo") as f:
        cpu = next((line.split(":")[1].strip() for line in f if line.startswith("model name")), "unknown")
    return f"{cpu}, {os.cpu_count()} logical CPUs, {platform.system()} {platform.release()}, " \
           f"Python {platform.python_version()}, SQLite {sqlite3.sqlite_version}"


def throughput(executors: int, tool_delay: float) -> float:
    """Sessions per second: 'quick' sessions (2 tool calls each) from first admission to all ended."""
    config = ManagerConfig(max_live_per_tenant=10_000, lease_seconds=3.0)
    with tempfile.TemporaryDirectory() as workdir, Cluster(workdir, AGENTS, executors, config, tool_delay, 0.5) as c:
        start = time.time()
        for i in range(SESSIONS):
            c.manager.start_session(f"tenant-{i % 10}", "quick")
        c.wait_for(c.all_ended, "sessions to end", timeout=300)
        return SESSIONS / (time.time() - start)


def resume_latencies(lease: float, heartbeat: float, trials: int) -> list[float]:
    """Seconds from SIGKILL of an executor to the first log entry written for each of its
    sessions by a new epoch. Polled every ~5 ms, so each value has that much granularity."""
    config = ManagerConfig(max_live_per_tenant=10_000, step_cap=10**9, run_limit_seconds=10**6, lease_seconds=lease)
    samples = []
    with tempfile.TemporaryDirectory() as workdir, Cluster(workdir, AGENTS, 4, config, 0.02, heartbeat) as c:
        for i in range(40):
            c.manager.start_session("t", "poison")  # never ends by itself: always work to displace
        time.sleep(1.0)
        rng = random.Random(0)
        for _ in range(trials):
            victim = rng.choice(list(c.workers))
            pending = {r["id"]: r["epoch"] for r in c.conn.execute(
                "SELECT id, epoch FROM sessions WHERE executor_id = ? AND state = 'running'", (victim,))}
            killed_at = c.kill_executor(victim)
            c.spawn_executor()
            while pending:
                for sid, old_epoch in list(pending.items()):
                    newest = c.conn.execute("SELECT MAX(epoch) FROM session_log WHERE session_id = ?", (sid,)).fetchone()[0]
                    if newest > old_epoch:
                        samples.append(time.time() - killed_at)
                        del pending[sid]
                if time.time() - killed_at > lease * 10 + 10:
                    raise SystemExit(f"sessions never resumed: {pending}")
                time.sleep(0.005)
            time.sleep(1.0)  # let the replacement settle before the next kill
    return samples


def percentile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    return ordered[math.ceil(p / 100 * len(ordered)) - 1]  # nearest rank


def main():
    print("machine:", machine())
    for delay in THROUGHPUT_DELAYS:
        print(f"\nTHROUGHPUT: {SESSIONS} 'quick' sessions (lookup, send_email, finish), tool delay {delay}s, "
              f"manager reconcile every 0.05s, executor poll 0.02s, {REPEATS} runs each")
        for n in EXECUTOR_COUNTS:
            runs = [throughput(n, delay) for _ in range(REPEATS)]
            print(f"  {n} executors: {statistics.median(runs):6.1f} sessions/s median "
                  f"(runs: {', '.join(f'{r:.1f}' for r in runs)})", flush=True)

    print("\nDEATH TO RESUME: 4 executors, 40 never-ending sessions, tool delay 0.02s, one executor SIGKILLed per trial")
    for lease, heartbeat in ((0.6, 0.1), (3.0, 0.5)):
        samples = resume_latencies(lease, heartbeat, trials=8)
        print(f"  lease {lease}s, heartbeat {heartbeat}s: n={len(samples)} sessions over 8 kills, "
              f"p50 {percentile(samples, 50):.2f}s, p95 {percentile(samples, 95):.2f}s, max {max(samples):.2f}s")


if __name__ == "__main__":
    main()
