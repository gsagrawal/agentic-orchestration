"""Step 8: many sessions, several worker processes, workers SIGKILLed at random times.

Needs the Temporal dev server: `make test-integration`. The seed fixes which worker dies and when
(the gaps between kills), not the exact interleaving, which depends on timing.
"""
import pytest

from agent_runtime.chaos import KINDS, run_chaos
from agent_runtime.invariants import check_invariants

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("seed", [1, 2])
async def test_invariants_hold_under_random_worker_kills(dev_env, tmp_path, seed):
    report = await run_chaos(
        dev_env.client, dev_env.client.service_client.config.target_host, str(tmp_path),
        n_sessions=24, n_workers=4, seed=seed,
        tool_delay_s=1.0, kill_gap_s=(1.5, 3.5),  # long calls, frequent kills: kills land mid-call
    )
    violations = check_invariants(
        report.results, {sid: a for sid, a in report.agent_of.items()}, report.tools, report.sandbox, report.notifier
    )
    assert violations == []

    # The test is only meaningful if it really killed workers and calls really were retried.
    assert report.kills >= 2
    retried = [k for k in report.tools.ledger.counts() if len(report.tools.attempts(k)) > 1]
    assert retried, "no tool call was ever retried: the kills never landed mid-call"

    # Every kind of session reached its expected end.
    by_kind = {}
    for sid, r in report.results.items():
        by_kind.setdefault(report.agent_of[sid].agent_id, set()).add(r.status)
    assert by_kind["poison"] == {"step_cap"}
    assert by_kind["asks-and-expires"] == {"expired"}
    assert all(by_kind[k] == {"finished"} for k in KINDS if k not in ("poison", "asks-and-expires"))
