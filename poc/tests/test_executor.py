"""agent-executor stub: grants, step cap, lazy sandbox, session log."""
import json
import uuid
import subprocess

from agent_executor import AgentExecutor, ToolCall
from agent_network_proxy.registry import Caller
from conftest import GRANTS


def _executor(proxied, tmp_path, **kw):
    ctl, _ = proxied()    # reuse the fixture's wiring (and its cleanup) for a controller
    return ctl, AgentExecutor(ctl, GRANTS, tmp_path / "session.jsonl", **kw)


def _sandboxes():
    return subprocess.run(["docker", "ps", "-q", "--filter", "label=agent-sandbox=1"],
                          capture_output=True, text=True).stdout.split()


def test_runs_a_script_logs_every_step_and_cleans_up(proxied, tmp_path):
    _, ex = _executor(proxied, tmp_path)
    caller = Caller(f"s-exec-{uuid.uuid4().hex[:8]}", "agent-a", "tenant-a", 1)
    results = ex.run(caller, [ToolCall("bash", "echo hi > /work/a.txt"), ToolCall("python", "print(open('/work/a.txt').read().strip())")],
                     tmp_path / "wd")
    assert [r["result"]["exit_code"] for r in results] == [0, 0]
    assert results[1]["result"]["stdout"].strip() == "hi"          # the same sandbox: workspace survived between calls
    log = [json.loads(l) for l in (tmp_path / "session.jsonl").read_text().splitlines()]
    assert [(l["step"], l["tool"]) for l in log] == [(1, "bash"), (2, "python")]
    assert {l["tenant"] for l in log} == {"tenant-a"}


def test_ungranted_tool_is_refused_and_no_sandbox_is_created(proxied, tmp_path):
    _, ex = _executor(proxied, tmp_path)
    before = set(_sandboxes())
    results = ex.run(Caller(f"s-deny-{uuid.uuid4().hex[:8]}", "agent-no-net", "tenant-a", 1), [ToolCall("python", "print(1)")], tmp_path / "wd")
    assert results[0]["result"] == {"error": "tool 'python' not granted"}
    assert set(_sandboxes()) == before                              # lazy: nothing ran, so nothing was created


def test_step_cap_stops_a_runaway_loop(proxied, tmp_path):
    _, ex = _executor(proxied, tmp_path, step_cap=3)
    results = ex.run(Caller(f"s-cap-{uuid.uuid4().hex[:8]}", "agent-a", "tenant-a", 1), [ToolCall("bash", "true")] * 10, tmp_path / "wd")
    assert len(results) == 4 and results[-1]["result"] == {"error": "step cap reached"}
    assert sum(1 for r in results if r["result"].get("exit_code") == 0) == 3


def test_timeout_in_a_step_is_reported_and_the_next_step_gets_a_fresh_sandbox(proxied, tmp_path):
    _, ex = _executor(proxied, tmp_path, timeout_s=2)
    results = ex.run(Caller(f"s-to-{uuid.uuid4().hex[:8]}", "agent-a", "tenant-a", 1),
                     [ToolCall("bash", "sleep 30"), ToolCall("bash", "echo ok")], tmp_path / "wd")
    assert results[0]["result"]["timed_out"] and results[1]["result"]["stdout"] == "ok\n"
