"""agent-executor stub: grants, step cap, lazy sandbox, session log."""
import json
import subprocess
import uuid

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


def test_works_with_a_controller_that_has_no_proxy(tmp_path):
    from sandbox_controller import SandboxController
    ex = AgentExecutor(SandboxController(), GRANTS, tmp_path / "log.jsonl")
    results = ex.run(Caller(f"s-np-{uuid.uuid4().hex[:8]}", "agent-no-net", "tenant-a", 1), [ToolCall("bash", "echo ok")], tmp_path / "wd")
    assert results[0]["result"]["stdout"] == "ok\n"


def test_session_deadline_ends_the_session(proxied, tmp_path):
    _, ex = _executor(proxied, tmp_path, deadline_s=4)
    results = ex.run(Caller(f"s-dl-{uuid.uuid4().hex[:8]}", "agent-a", "tenant-a", 1),
                     [ToolCall("bash", "echo one"), ToolCall("bash", "sleep 30"), ToolCall("bash", "echo three")], tmp_path / "wd")
    assert results[0]["result"]["stdout"] == "one\n"
    assert results[1]["result"]["timed_out"]                       # cut off by the session deadline, not by the 30 s step timeout
    assert results[2]["result"] == {"error": "session deadline reached"}


def test_a_lost_sandbox_is_replaced_and_the_unstarted_command_runs_once(proxied, tmp_path):
    from sandbox_controller import Limits
    _, ex = _executor(proxied, tmp_path, limits=Limits(deadline_s=3))     # the sandbox itself expires after 3 s
    results = ex.run(Caller(f"s-lost-{uuid.uuid4().hex[:8]}", "agent-a", "tenant-a", 1),
                     [ToolCall("bash", "sleep 5"), ToolCall("bash", "echo recovered")], tmp_path / "wd")
    assert results[0]["result"]["exit_code"] != 0 and not results[0]["result"]["timed_out"]   # died mid-run: reported, not retried
    assert results[1]["result"]["stdout"] == "recovered\n"                                   # next step got a fresh sandbox


def _labelled():
    """Every sandbox container and sandbox network that currently exists."""
    docker = lambda *a: set(subprocess.run(["docker", *a, "--filter", "label=agent-sandbox=1", "-q"],
                                           capture_output=True, text=True).stdout.split())
    return docker("ps", "-a"), docker("network", "ls")


def test_an_unexpected_failure_does_not_leak_the_sandbox(proxied, tmp_path, monkeypatch):
    ctl, ex = _executor(proxied, tmp_path)
    def boom(*a, **kw):
        raise OSError("docker client vanished")
    monkeypatch.setattr(ctl, "exec", boom)
    before = _labelled()
    results = ex.run(Caller(f"s-leak-{uuid.uuid4().hex[:8]}", "agent-a", "tenant-a", 1),
                     [ToolCall("bash", "true"), ToolCall("bash", "true")], tmp_path / "wd")
    assert all("docker client vanished" in r["result"]["error"] for r in results)    # reported, not raised
    assert _labelled() == before          # the sandbox the executor created is stopped and its network removed


def test_a_tool_the_executor_cannot_run_is_rejected_up_front(tmp_path):
    import pytest
    from sandbox_controller import SandboxController
    grants = tmp_path / "grants.json"
    grants.write_text(json.dumps({"agents": {"a": {"tenant": "t", "tools": ["bash", "perl"], "destinations": {}}}}))
    with pytest.raises(ValueError, match="perl"):
        AgentExecutor(SandboxController(), grants, tmp_path / "log").run(Caller("s", "a", "t", 1), [], tmp_path / "wd")


def test_an_unknown_agent_is_a_clear_error(tmp_path):
    import pytest
    from sandbox_controller import SandboxController
    with pytest.raises(ValueError, match="unknown agent"):
        AgentExecutor(SandboxController(), GRANTS, tmp_path / "log").run(Caller("s", "agent-typo", "tenant-a", 1), [], tmp_path / "wd")


def test_a_cleanup_failure_does_not_discard_the_results(proxied, tmp_path, monkeypatch):
    ctl, ex = _executor(proxied, tmp_path)
    real_stop = ctl.stop
    def failing_stop(sandbox):
        real_stop(sandbox)
        raise OSError("registry unwritable")
    monkeypatch.setattr(ctl, "stop", failing_stop)
    results = ex.run(Caller(f"s-cl-{uuid.uuid4().hex[:8]}", "agent-a", "tenant-a", 1), [ToolCall("bash", "echo ok")], tmp_path / "wd")
    assert results[0]["result"]["stdout"] == "ok\n"                       # the work that was done is still returned
    assert "cleanup failed" in results[-1]["result"]["error"]


def test_a_sandbox_whose_stop_failed_is_not_marked_stopped(proxied, monkeypatch):
    import pytest
    ctl, sbx = proxied()
    monkeypatch.setattr(proxied.registry, "unregister", lambda *a: (_ for _ in ()).throw(OSError("boom")))
    with pytest.raises(OSError):
        ctl.stop(sbx)
    assert sbx.stopped is False          # so the executor knows to try again
    monkeypatch.undo()
    ctl.stop(sbx)
    assert sbx.stopped is True
