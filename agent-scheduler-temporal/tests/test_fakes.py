"""The faked notifier and sandbox controller keep the promises the workflow relies on."""
from agent_scheduler_temporal.notifier import FakeNotifier
from agent_scheduler_temporal.sandbox import FakeSandboxController


def test_notification_is_sent_once_per_key(tmp_path):
    n = FakeNotifier(str(tmp_path / "n.db"))
    assert n.notify("s1:3", "ok?") is True
    assert n.notify("s1:3", "ok?") is False  # a retried request_human sends nothing
    assert n.notify("s1:4", "again?") is True
    assert n.sent() == ["s1:3", "s1:4"]


def test_sandbox_reports_creation_only_when_it_is_new(tmp_path):
    s = FakeSandboxController(str(tmp_path / "s.db"))
    assert s.ensure("a") is True
    assert s.ensure("a") is False
    s.lose("a")  # died on its own
    assert s.ensure("a") is True
    s.stop("a")
    assert s.live_sessions() == []
    assert s.ensure("a") is True
