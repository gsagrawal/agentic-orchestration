"""A local cluster for chaos tests, the demo and the benchmark: the manager's reconcile loop
in a thread of this process, executors as separate OS processes you can SIGKILL."""

import os
import signal
import threading
import time
from pathlib import Path

from .agent_store import AgentStore
from .db import connect, init_db
from .executor import start_worker
from .session_manager import ManagerConfig, SessionManager
from .state_table import TERMINAL_STATES


class Cluster:
    def __init__(self, workdir, agents_path: str, executors: int, config: ManagerConfig = ManagerConfig(),
                 tool_delay: float = 0.0, heartbeat_interval: float = 0.1, reconcile_interval: float = 0.05):
        self.db_path = str(Path(workdir) / "orch.db")
        self.agents_path, self.config = agents_path, config
        self.tool_delay, self.heartbeat_interval = tool_delay, heartbeat_interval
        self.reconcile_interval = reconcile_interval
        self._initial_executors = executors
        self.workers: dict[str, object] = {}
        self._spawned = 0
        self._stop = threading.Event()
        self.manager_error: Exception | None = None

    def __enter__(self):
        init_db(self.db_path)
        self.conn = connect(self.db_path)
        self.manager = SessionManager(self.db_path, AgentStore(self.agents_path), self.config)
        # The reconcile loop gets its own manager: a SQLite connection belongs to one thread.
        self._thread = threading.Thread(target=self._reconcile_loop, daemon=True)
        self._thread.start()
        for _ in range(self._initial_executors):
            self.spawn_executor()
        self.wait_for(lambda: self._registered() >= self._initial_executors, "executors to register")
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join()
        for proc in self.workers.values():
            proc.kill()
            proc.join()

    def _reconcile_loop(self):
        manager = SessionManager(self.db_path, AgentStore(self.agents_path), self.config)
        try:
            while not self._stop.is_set():
                manager.reconcile_once()
                self._stop.wait(self.reconcile_interval)
        except Exception as e:  # surfaced by wait_for, so a dead manager fails the run loudly
            self.manager_error = e

    def spawn_executor(self) -> str:
        self._spawned += 1
        executor_id = f"ex-{self._spawned}"
        self.workers[executor_id] = start_worker(
            self.db_path, self.agents_path, executor_id, self.tool_delay, self.heartbeat_interval)
        return executor_id

    def kill_executor(self, executor_id: str) -> float:
        """SIGKILL the executor. Returns the time just before the kill."""
        proc = self.workers.pop(executor_id)
        killed_at = time.time()
        proc.kill()
        proc.join()
        return killed_at

    def freeze_executor(self, executor_id: str) -> None:
        """SIGSTOP: the process stops, so it misses heartbeats but is not dead."""
        os.kill(self.workers[executor_id].pid, signal.SIGSTOP)

    def unfreeze_executor(self, executor_id: str) -> None:
        """SIGCONT: it wakes up holding whatever it held before, which may now be a zombie."""
        os.kill(self.workers[executor_id].pid, signal.SIGCONT)

    def _registered(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM executors").fetchone()[0]

    def all_ended(self) -> bool:
        marks = ",".join("?" * len(TERMINAL_STATES))
        query = f"SELECT COUNT(*) FROM sessions WHERE state NOT IN ({marks})"
        return self.conn.execute(query, TERMINAL_STATES).fetchone()[0] == 0

    def wait_for(self, condition, what: str, timeout: float = 60.0) -> None:
        deadline = time.time() + timeout
        while not condition():
            if self.manager_error:
                raise AssertionError(f"the manager loop died: {self.manager_error!r}")
            if time.time() > deadline:
                raise AssertionError(f"timed out waiting for {what}")
            time.sleep(0.02)
