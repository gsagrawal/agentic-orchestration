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
