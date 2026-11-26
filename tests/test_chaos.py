"""Chaos: many sessions, several executors, executors disturbed at random times, either
SIGKILLed (gone for good) or frozen with SIGSTOP past the lease and then resumed with
SIGCONT (a zombie that wakes up after its sessions were reassigned).

Whatever the schedule, at the end every session has ended the way its agent should end,
and the invariants hold: no tool ran twice for a (session, step), no log entry or effect has a
stale epoch, every log is well formed. The schedule is seeded; the timing of the
processes is not, so a failing seed is a lead to investigate, not a guaranteed replay.
"""

import random
import time
from pathlib import Path

import pytest

from orchestrator import invariants
from orchestrator.cluster import Cluster
from orchestrator.session_manager import ManagerConfig

AGENTS = str(Path(__file__).parent.parent / "scripts" / "agents.json")

# agent -> the state its sessions must end in
EXPECTED_END = {"quick": "done", "set-value": "done", "ask-human": "expired", "poison": "stopped"}

SESSIONS, EXECUTORS, MIN_KILLS = 60, 4, 5
# ASSUMPTIONS for this test: short timers so it finishes in seconds.
CONFIG = ManagerConfig(lease_seconds=0.6, wait_timeout_seconds=1.0, step_cap=6, run_limit_seconds=120,
                       max_live_per_tenant=1000)


@pytest.mark.parametrize("seed", [1, 2])
def test_random_executor_kills_never_break_the_invariants(tmp_path, seed):
    rng = random.Random(seed)
    with Cluster(tmp_path, AGENTS, EXECUTORS, CONFIG, tool_delay=0.1) as cluster:
        agents = {}
        for i in range(SESSIONS):
            agent = rng.choice(list(EXPECTED_END))
            agents[cluster.manager.start_session(f"tenant-{i % 3}", agent)] = agent

        started = time.time()
        kills = freezes = 0
        next_event = started + rng.uniform(0.1, 0.4)
        thaw_at: dict[str, float] = {}  # frozen executor -> when to resume it
        while not cluster.all_ended():
            now = time.time()
            for executor_id, when in list(thaw_at.items()):
                if now >= when:
                    cluster.unfreeze_executor(executor_id)
                    del thaw_at[executor_id]
            if now >= next_event:
                executor_id = rng.choice(list(cluster.workers))
                if rng.random() < 0.7 or executor_id in thaw_at:
                    thaw_at.pop(executor_id, None)
                    cluster.kill_executor(executor_id)
                    cluster.spawn_executor()  # keep the cluster at full strength
                    kills += 1
                else:
                    cluster.freeze_executor(executor_id)
                    thaw_at[executor_id] = now + rng.uniform(0.9, 1.5)  # longer than the lease
                    freezes += 1
                next_event = now + rng.uniform(0.1, 0.4)
            cluster.wait_for(lambda: True, "")  # raises if the manager loop died
            time.sleep(0.02)
            assert time.time() < started + 90, "sessions did not all end"

        assert invariants.check(cluster.conn) == []
        ended = {r["id"]: r["state"] for r in cluster.conn.execute("SELECT id, state FROM sessions")}
        assert {sid: ended[sid] for sid in agents} == {sid: EXPECTED_END[a] for sid, a in agents.items()}

        # The run must actually have been disturbed, or it proves nothing.
        reassigned = cluster.conn.execute("SELECT COUNT(*) FROM sessions WHERE epoch > 1").fetchone()[0]
        assert kills >= MIN_KILLS and freezes >= 1, f"{kills} kills, {freezes} freezes: too short to be a real test"
        assert reassigned > 0, "no session was ever moved to another executor"
