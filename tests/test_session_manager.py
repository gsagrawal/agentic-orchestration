"""Session manager: admission (quota, budget, pinning, deadlines) and assignment."""

import json
import threading

import pytest

from orchestrator import state_table
from orchestrator.agent_store import AgentStore
from orchestrator.errors import BudgetExceeded, QuotaExceeded, UnknownAgent
from orchestrator.executor import Executor
from orchestrator.session_manager import ManagerConfig, SessionEngine, SessionManager


def make_manager(db_path, agents_path, now=1000.0, **config):
    return SessionManager(db_path, AgentStore(agents_path), ManagerConfig(**config), clock=lambda: now)


def test_admission_pins_version_and_sets_deadline_and_step_cap(conn, db_path, tmp_path):
    agents = {"a": {"1": {"script": []}, "2": {"script": []}}}
    path = tmp_path / "two-versions.json"
    path.write_text(json.dumps(agents))
    mgr = make_manager(db_path, str(path), now=1000.0, step_cap=7, run_limit_seconds=60)
    sid = mgr.start_session("t1", "a")
    row = state_table.get_session(conn, sid)
    assert (row["state"], row["agent_version"], row["step_cap"], row["run_deadline"]) == ("created", 2, 7, 1060.0)
    assert (row["epoch"], row["executor_id"], row["wait_deadline"]) == (0, None, None)


def test_unknown_agent_is_refused(db_path, agents_path):
    with pytest.raises(UnknownAgent):
        make_manager(db_path, agents_path).start_session("t1", "nope")


def test_tenant_over_quota_is_refused_and_other_tenants_are_unaffected(conn, db_path, agents_path):
    mgr = make_manager(db_path, agents_path, max_live_per_tenant=2)
    mgr.start_session("noisy", "two-tools")
    mgr.start_session("noisy", "two-tools")
    with pytest.raises(QuotaExceeded):
        mgr.start_session("noisy", "two-tools")
    mgr.start_session("quiet", "two-tools")  # a different tenant still gets in
    assert state_table.count_live(conn, "noisy") == 2
    assert state_table.count_live(conn, "quiet") == 1


def test_quota_can_be_set_per_tenant(db_path, agents_path):
    mgr = make_manager(db_path, agents_path, max_live_per_tenant=1, tenant_overrides={"big": 3})
    for _ in range(3):
        mgr.start_session("big", "two-tools")
    mgr.start_session("small", "two-tools")
    with pytest.raises(QuotaExceeded):
        mgr.start_session("small", "two-tools")


def test_finished_sessions_free_quota_but_waiting_ones_do_not(conn, db_path, agents_path):
    mgr = make_manager(db_path, agents_path, max_live_per_tenant=2)
    a = mgr.start_session("t", "two-tools")
    b = mgr.start_session("t", "two-tools")
    conn.execute("UPDATE sessions SET state = 'waiting' WHERE id = ?", (a,))
    with pytest.raises(QuotaExceeded):
        mgr.start_session("t", "two-tools")  # a waiting session still counts
    conn.execute("UPDATE sessions SET state = 'done' WHERE id = ?", (b,))
    mgr.start_session("t", "two-tools")


def test_concurrent_admissions_cannot_exceed_the_quota(db_path, agents_path):
    """Eight threads (own connections) race for 3 slots: exactly 3 get in."""
    admitted, refused, errors = [], [], []

    def admit():
        try:
            admitted.append(make_manager(db_path, agents_path, max_live_per_tenant=3).start_session("t", "two-tools"))
        except QuotaExceeded:
            refused.append(1)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=admit) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert (len(admitted), len(refused)) == (3, 5)


def test_over_budget_tenant_is_refused(conn, db_path, agents_path):
    conn.execute("INSERT INTO tenant_budget (tenant, over_budget) VALUES ('broke', 1)")
    mgr = make_manager(db_path, agents_path)
    with pytest.raises(BudgetExceeded):
        mgr.start_session("broke", "two-tools")
    mgr.start_session("fine", "two-tools")


def test_assignment_bumps_epoch_and_spreads_sessions_over_executors(conn, db_path, agents_path):
    for ex in ("ex-1", "ex-2"):
        state_table.register_executor(conn, ex, 0)
    mgr = make_manager(db_path, agents_path)
    ids = [mgr.start_session("t", "two-tools") for _ in range(4)]
    mgr.reconcile_once()
    rows = [state_table.get_session(conn, s) for s in ids]
    assert [r["state"] for r in rows] == ["running"] * 4
    assert [r["epoch"] for r in rows] == [1] * 4
    assert sorted(r["executor_id"] for r in rows) == ["ex-1", "ex-1", "ex-2", "ex-2"]


def test_sessions_wait_in_created_until_an_executor_exists(conn, db_path, agents_path):
    mgr = make_manager(db_path, agents_path)
    sid = mgr.start_session("t", "two-tools")
    mgr.reconcile_once()
    assert mgr.get_status(sid)["state"] == "created"
    state_table.register_executor(conn, "ex-1", 0)
    mgr.reconcile_once()
    assert mgr.get_status(sid)["state"] == "running"


def test_reconcile_is_idempotent(conn, db_path, agents_path):
    state_table.register_executor(conn, "ex-1", 0)
    mgr = make_manager(db_path, agents_path)
    sid = mgr.start_session("t", "two-tools")
    mgr.reconcile_once()
    mgr.reconcile_once()
    assert mgr.get_status(sid)["epoch"] == 1  # a second pass must not reassign


def test_admit_assign_and_run_end_to_end(conn, db_path, agents_path):
    ex = Executor(db_path, agents_path, "ex-1")  # registers itself
    mgr = make_manager(db_path, agents_path)
    sid = mgr.start_session("t", "two-tools")
    mgr.reconcile_once()
    for _ in range(10):
        ex.tick()
    assert mgr.get_status(sid)["state"] == "done"


def test_session_manager_satisfies_the_engine_interface(db_path, agents_path):
    assert isinstance(make_manager(db_path, agents_path), SessionEngine)


def test_reconcile_skips_a_session_that_ended_between_listing_and_assigning(conn, db_path, agents_path, monkeypatch):
    state_table.register_executor(conn, "ex-1", 0)
    mgr = make_manager(db_path, agents_path)
    ended = mgr.start_session("t", "two-tools")
    live = mgr.start_session("t", "two-tools")
    conn.execute("UPDATE sessions SET state = 'cancelled' WHERE id = ?", (ended,))
    monkeypatch.setattr(state_table, "unassigned_sessions", lambda c: [ended, live])  # the stale listing
    mgr.reconcile_once()
    assert mgr.get_status(ended)["state"] == "cancelled"
    assert mgr.get_status(live)["state"] == "running"
