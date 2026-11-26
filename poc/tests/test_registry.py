"""Caller registry: concurrent writers must not lose updates, and readers always see the latest write."""
import os
import threading

from agent_network_proxy.registry import Caller, Registry


def test_concurrent_writers_lose_nothing(tmp_path):
    reg = Registry(tmp_path / "registry.json")
    threads = [threading.Thread(target=reg.register, args=(f"10.0.0.{i}", Caller(f"s{i}", "a", "t", 1), f"sbx{i}"))
               for i in range(40)]
    [t.start() for t in threads]; [t.join() for t in threads]
    assert all(reg.lookup(f"10.0.0.{i}") for i in range(40))


def test_a_write_is_visible_even_if_the_file_timestamp_does_not_change(tmp_path):
    reg = Registry(tmp_path / "registry.json")
    stamp = (1_000_000_000, 1_000_000_000)
    reg.register("10.0.0.1", Caller("s", "a", "t", 1), "sbx1")
    os.utime(reg.path, ns=stamp)
    assert reg.lookup("10.0.0.1")
    reg.unregister("sbx1")
    os.utime(reg.path, ns=stamp)           # a coarse-timestamp filesystem: same mtime as before
    assert reg.lookup("10.0.0.1") is None  # the proxy must not keep trusting an unregistered caller


def test_a_failed_write_leaves_no_temp_file_and_keeps_the_old_registry(tmp_path, monkeypatch):
    import pytest
    reg = Registry(tmp_path / "registry.json")
    reg.register("10.0.0.1", Caller("s", "a", "t", 1), "sbx1")
    def boom(*a, **kw):
        raise OSError("disk full")
    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        reg.register("10.0.0.2", Caller("s2", "a", "t", 1), "sbx2")
    monkeypatch.undo()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["registry.json", "registry.json.lock"]
    assert reg.lookup("10.0.0.1") and reg.lookup("10.0.0.2") is None


def test_a_wrongly_shaped_registry_is_an_error_not_a_crash(tmp_path):
    import pytest
    from agent_network_proxy.registry import RegistryError
    reg = Registry(tmp_path / "registry.json")
    for bad in ("{}", "[]", '{"callers": [], "epochs": {}}'):
        reg.path.write_text(bad)
        with pytest.raises(RegistryError):
            reg.newest_epoch("t/s")


def test_registering_a_claim_that_was_superseded_is_refused(tmp_path):
    import pytest
    from agent_network_proxy.registry import Superseded
    reg = Registry(tmp_path / "registry.json")
    caller = Caller("s", "a", "t", 1)
    with reg.transaction() as data:                                  # the claim, as the controller's fence writes it
        data["callers"]["pending:sbx1"] = {"session": "s", "agent": "a", "tenant": "t", "epoch": 1, "sandbox": "sbx1", "pending": 1e12}
        data["epochs"][caller.key] = 1
    reg.register("10.0.0.1", caller, "sbx1", claimed=True)           # claim intact: fine
    with reg.transaction() as data:
        data["callers"]["pending:sbx2"] = {"session": "s", "agent": "a", "tenant": "t", "epoch": 1, "sandbox": "sbx2", "pending": 1e12}
        data["epochs"][caller.key] = 2                               # a newer executor has fenced it
    with pytest.raises(Superseded):
        reg.register("10.0.0.2", caller, "sbx2", claimed=True)
    reg.unregister("sbx2")
    with pytest.raises(Superseded):                                  # and a claim that is simply gone
        reg.register("10.0.0.3", Caller("s", "a", "t", 2), "sbx3", claimed=True)


def test_binary_garbage_is_an_error_and_clear_repairs_it(tmp_path):
    import pytest
    from agent_network_proxy.registry import RegistryError
    reg = Registry(tmp_path / "registry.json")
    reg.path.write_bytes(b"\xff\xfe\x00 not utf-8 \x80\x81")
    with pytest.raises(RegistryError):
        reg.lookup("10.0.0.1")
    reg.clear()                                          # crash recovery must handle this too
    assert reg.lookup("10.0.0.1") is None
