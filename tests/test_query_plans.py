"""The hot queries must use an index, not scan every session ever created."""

import time

import pytest

from orchestrator import state_table


def plan_of_last_select(conn, call):
    """Run `call`, take the last SELECT it issued, and return SQLite's plan for it."""
    seen = []
    conn.set_trace_callback(seen.append)
    call()
    conn.set_trace_callback(None)
    select = [sql for sql in seen if sql.lstrip().upper().startswith("SELECT")][-1]
    return " | ".join(row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + select))


@pytest.mark.parametrize("name, call", [
    ("assigned_to (every executor, every tick)", lambda c: state_table.assigned_to(c, "ex-1")),
    ("count_live (every admission)", lambda c: state_table.count_live(c, "t")),
    ("sessions_needing_executor (every reconcile pass)", lambda c: state_table.sessions_needing_executor(c, time.time())),
])
def test_hot_queries_do_not_scan_the_sessions_table(conn, name, call):
    plan = plan_of_last_select(conn, lambda: call(conn))
    # SEARCH is an index lookup; SCAN reads every row (the small `executors` table may be scanned)
    assert "SEARCH sessions USING" in plan and "SCAN sessions" not in plan, f"{name}: {plan}"
