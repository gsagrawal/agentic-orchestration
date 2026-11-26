"""make demo: admit sessions, kill an executor in the middle of tool calls, show the outcome."""

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator import invariants  # noqa: E402
from orchestrator.cluster import Cluster  # noqa: E402
from orchestrator.errors import QuotaExceeded  # noqa: E402
from orchestrator.session_manager import ManagerConfig  # noqa: E402
from orchestrator.tool_service import effect_counts  # noqa: E402

AGENTS = str(Path(__file__).parent / "agents.json")
# Short timers so the demo takes seconds (assumptions, not recommendations).
CONFIG = ManagerConfig(max_live_per_tenant=8, step_cap=5, lease_seconds=1.0, wait_timeout_seconds=1.5)

PLAN = [("acme", "quick", 5), ("acme", "set-value", 2), ("acme", "ask-human", 1),
        ("globex", "quick", 3), ("globex", "poison", 1)]


def main():
    started = time.time()

    def say(text):
        print(f"[{time.time() - started:5.1f}s] {text}")

    with tempfile.TemporaryDirectory() as workdir, Cluster(workdir, AGENTS, 3, CONFIG, tool_delay=0.1) as cluster:
        agents = {}
        for tenant, agent, count in PLAN:
            for _ in range(count):
                agents[cluster.manager.start_session(tenant, agent)] = (tenant, agent)
        say(f"admitted {len(agents)} sessions for 2 tenants on 3 executors")
        try:
            cluster.manager.start_session("acme", "quick")  # acme already has 8 live sessions
        except QuotaExceeded:
            say("acme's 9th session refused (quota 8); globex is unaffected")

        time.sleep(0.5)
        victim = max(cluster.workers, key=lambda e: len(running_on(cluster, e)))
        stranded = running_on(cluster, victim)
        say(f"kill -9 {victim}, which is running {len(stranded)} sessions")
        cluster.kill_executor(victim)
        cluster.spawn_executor()

        cluster.wait_for(cluster.all_ended, "all sessions to end")
        say("all sessions ended")

        print("\nsession   agent      end state  epoch  steps  note")
        for row in cluster.conn.execute("SELECT * FROM sessions ORDER BY rowid"):
            _, agent = agents[row["id"]]
            note = row["stop_reason"] or ("moved after the kill" if row["id"] in stranded else "")
            print(f"{row['id'][:8]}  {agent:<10} {row['state']:<10} {row['epoch']:<6} {row['step_count']:<6} {note}")

        effects = effect_counts(cluster.conn)
        print(f"\ntool effects: {sum(effects.values())} real runs over {len(effects)} (session, step) pairs; "
              f"most runs of any one step: {max(effects.values())}")
        unknown = cluster.conn.execute("SELECT COUNT(*) FROM session_log WHERE kind = 'tool_unknown'").fetchone()[0]
        print(f"calls the model was told 'outcome unknown' for: {unknown}")
        problems = invariants.check(cluster.conn)
        print("invariants:", "all hold" if not problems else problems)
        return 1 if problems else 0


def running_on(cluster, executor_id):
    rows = cluster.conn.execute(
        "SELECT id FROM sessions WHERE executor_id = ? AND state = 'running'", (executor_id,)).fetchall()
    return [r["id"] for r in rows]


if __name__ == "__main__":
    sys.exit(main())
