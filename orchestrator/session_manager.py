"""agent-session-manager: admits sessions, assigns each to exactly one executor.

Callers depend on SessionEngine only. The state table plus reconciler below is
one implementation; a workflow engine such as Temporal could be another without
changing callers. (It would not remove the "outcome unknown" rule: its
activities are also at-least-once.)

The manager only READS budget status (tenant_budget); it never sums usage.
"""

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Protocol, runtime_checkable

from . import state_table
from .agent_store import AgentStore
from .db import connect, write_txn
from .errors import BudgetExceeded, QuotaExceeded


@runtime_checkable
class SessionEngine(Protocol):
    def start_session(self, tenant: str, agent_id: str) -> str: ...
    def get_status(self, session_id: str) -> dict: ...


@dataclass(frozen=True)
class ManagerConfig:
    # The defaults are ASSUMPTIONS for the demo, not measured or derived from anything.
    max_live_per_tenant: int = 10
    tenant_overrides: dict[str, int] = field(default_factory=dict)  # tenant -> its own limit
    step_cap: int = 50
    run_limit_seconds: float = 300.0   # active-run limit, counted from admission
    wait_timeout_seconds: float = 3600.0  # how long a session may wait for a human
    # An executor is dead if its last heartbeat is older than this. Must exceed the executor's
    # heartbeat interval plus its longest single loop iteration (a long tool call).
    lease_seconds: float = 3.0

    def quota_for(self, tenant: str) -> int:
        return self.tenant_overrides.get(tenant, self.max_live_per_tenant)


class SessionManager:
    def __init__(self, db_path: str, agents: AgentStore, config: ManagerConfig = ManagerConfig(),
                 clock: Callable[[], float] = time.time):
        self.conn = connect(db_path)
        self.agents = agents
        self.config = config
        self.clock = clock

    # --- admission -------------------------------------------------------------------

    def start_session(self, tenant: str, agent_id: str) -> str:
        """Admit a session: check budget and quota, pin the agent version, set deadline and step cap."""
        version = self.agents.latest_version(agent_id)  # raises UnknownAgent
        session_id = uuid.uuid4().hex
        # One transaction, so two concurrent admissions cannot both take the last slot.
        with write_txn(self.conn):
            if state_table.is_over_budget(self.conn, tenant):
                raise BudgetExceeded(tenant)
            if state_table.count_live(self.conn, tenant) >= self.config.quota_for(tenant):
                raise QuotaExceeded(tenant)
            state_table.create_session(
                self.conn, session_id, tenant, self.config.step_cap, agent_id, version,
                run_deadline=self.clock() + self.config.run_limit_seconds,
                wait_timeout=self.config.wait_timeout_seconds,
            )
        return session_id

    def get_status(self, session_id: str) -> dict:
        return dict(state_table.get_session(self.conn, session_id))

    # --- reconciler ------------------------------------------------------------------

    def reconcile_once(self) -> None:
        """Make reality match the desired state.

        1. A session past a deadline is ended (timers run first, so an overdue session is
           never handed to a new executor).
        2. Every other live session runs on a live executor. That covers first assignment and
           recovery from a dead executor alike; the manager does not need to know why an
           executor is gone.
        Assumes one manager process.
        """
        now = self.clock()
        state_table.expire_waiting(self.conn, now)
        state_table.stop_overdue_runs(self.conn, now)
        alive_since = now - self.config.lease_seconds
        for session_id in state_table.sessions_needing_executor(self.conn, alive_since):
            executor_id = state_table.least_loaded_executor(self.conn, alive_since)
            if executor_id is None:
                return  # no live executor; the sessions are retried on the next pass
            state_table.assign_if_needed(self.conn, session_id, executor_id, alive_since)

    def run_forever(self, stop: threading.Event, interval: float = 0.05) -> None:
        """Reconcile every `interval` seconds until `stop` is set."""
        while not stop.is_set():
            self.reconcile_once()
            stop.wait(interval)
