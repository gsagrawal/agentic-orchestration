"""agent-session-manager: admits sessions, assigns each to exactly one executor.

Callers depend on SessionEngine only. The state table plus reconciler below is
one implementation; a workflow engine such as Temporal could be another without
changing callers. (It would not remove the "outcome unknown" rule: its
activities are also at-least-once.)

The manager only READS budget status (tenant_budget); it never sums usage.
"""

import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Protocol

from . import state_table
from .agent_store import AgentStore
from .db import connect, write_txn
from .errors import BudgetExceeded, QuotaExceeded


class SessionEngine(Protocol):
    def start_session(self, tenant: str, agent_id: str) -> str: ...
    def get_status(self, session_id: str) -> dict: ...


@dataclass(frozen=True)
class ManagerConfig:
    # The defaults are ASSUMPTIONS for the demo, not measured or derived from anything.
    max_live_per_tenant: int = 10
    tenant_overrides: dict[str, int] = field(default_factory=dict)  # tenant -> its own limit
    step_cap: int = 50
    run_limit_seconds: float = 300.0

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
            )
        return session_id

    def get_status(self, session_id: str) -> dict:
        return dict(state_table.get_session(self.conn, session_id))

    # --- reconciler ------------------------------------------------------------------

    def reconcile_once(self) -> None:
        """Compare what should be true with what is, and fix it. Today: every admitted
        session must have an executor. Only one manager may run this (no leader election)."""
        for session_id in state_table.unassigned_sessions(self.conn):
            executor_id = state_table.least_loaded_executor(self.conn)
            if executor_id is None:
                return  # no executors yet; the sessions stay 'created' and are retried next time
            state_table.assign(self.conn, session_id, executor_id)

    def run_forever(self, interval: float = 0.05) -> None:
        while True:
            self.reconcile_once()
            time.sleep(interval)
