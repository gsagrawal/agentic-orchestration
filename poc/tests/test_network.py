"""Network: the sandbox reaches only the proxy, and the proxy enforces who, where, and how often."""
import json
import subprocess
from pathlib import Path
import threading
import time
import uuid

from conftest import AUDIT_LOG, STATE, STUB_LOG, fetch, lines, py
from agent_network_proxy.registry import Caller

GITHUB = "https://api.github.test/user"
SECRET = json.loads((STATE.parent / "config" / "secrets.json").read_text())["tenant-a"]["github"]


def _stub_ip():
    fmt = '{{(index .NetworkSettings.Networks "agentpoc_upstream").IPAddress}}'
    name = subprocess.run(["docker", "ps", "--filter", "label=com.docker.compose.service=stub", "--filter",
                           "label=com.docker.compose.project=agentpoc", "--format", "{{.Names}}"],
                          capture_output=True, text=True, check=True).stdout.split()[0]
    return subprocess.run(["docker", "inspect", "-f", fmt, name], capture_output=True, text=True, check=True).stdout.strip()


def test_direct_request_to_the_stub_fails(proxied):
    ctl, sbx = proxied()
    before = len(lines(STUB_LOG))
    r = py(ctl, sbx, f"import socket; socket.create_connection(('{_stub_ip()}', 443), 3)")
    assert r.exit_code != 0 and "unreachable" in r.stderr.lower()
    assert len(lines(STUB_LOG)) == before


def test_no_route_to_the_internet_and_no_dns(proxied):
    ctl, sbx = proxied()
    assert py(ctl, sbx, "import socket; socket.create_connection(('1.1.1.1', 443), 3)").exit_code != 0
    assert py(ctl, sbx, "import socket; socket.getaddrinfo('api.github.test', 443)").exit_code != 0


def test_the_proxy_is_reachable_and_forwards_granted_traffic(proxied):
    ctl, sbx = proxied()
    assert fetch(ctl, sbx, GITHUB).stdout.startswith("200 ")


def test_destination_not_in_grants_is_denied(proxied):
    ctl, sbx = proxied()
    out = fetch(ctl, sbx, "https://evil.test/steal").stdout
    assert "403" in out
    deny = [l for l in lines(AUDIT_LOG) if l["host"] == "evil.test"][-1]
    assert (deny["decision"], deny["reason"], deny["session"]) == ("deny", "destination not granted", sbx.caller.session)


def test_agent_with_no_grants_cannot_reach_anything(proxied):
    ctl, sbx = proxied(agent="agent-no-net")
    assert "403" in fetch(ctl, sbx, GITHUB).stdout


def test_caller_is_identified_by_address_not_by_claims(proxied):
    ctl, sbx = proxied()
    # The request claims to be another tenant/session; the proxy must still use the source address.
    code = ("import urllib.request as u\n"
            "r=u.Request('https://api.github.test/user',headers={'X-Tenant':'tenant-b','X-Session':'other'})\n"
            "print(u.urlopen(r,timeout=8).status)")
    assert py(ctl, sbx, code).stdout.strip() == "200"
    last = [l for l in lines(AUDIT_LOG) if l["decision"] == "allow"][-1]
    assert last["tenant"] == "tenant-a" and last["session"] == sbx.caller.session


def test_rate_limit_is_per_tenant_and_a_window(proxied):
    ctl, sbx = proxied(agent="agent-c", tenant="tenant-c")         # limit: 3 requests / 5 s
    outs = [fetch(ctl, sbx, GITHUB).stdout for _ in range(5)]
    assert all(o.startswith("200 ") for o in outs[:3])
    assert all("HTTPError 429" in o for o in outs[3:])
    ctl_a, a = proxied()                                            # another tenant is unaffected
    assert fetch(ctl_a, a, GITHUB).stdout.startswith("200 ")
    time.sleep(5.5)
    assert fetch(ctl, sbx, GITHUB).stdout.startswith("200 ")


def test_new_epoch_stops_the_old_sandbox(proxied):
    ctl, old = proxied(session="fence", epoch=1)
    _, new = proxied(session="fence", epoch=2)
    assert not old.alive and new.alive
    assert subprocess.run(["docker", "inspect", old.name], capture_output=True).returncode != 0
    callers = proxied.registry._read()["callers"]
    assert [c["epoch"] for c in callers.values() if c["session"] == new.caller.session] == [2]


def test_stale_epoch_cannot_start_a_sandbox(proxied):
    import pytest
    from sandbox_controller import SandboxError
    ctl, current = proxied(session="stale", epoch=5)
    with pytest.raises(SandboxError, match="stale epoch"):
        ctl.create(STATE / "x", Caller(current.caller.session, "agent-a", "tenant-a", 4))


def test_zombie_with_a_stale_epoch_is_rejected_by_the_proxy(proxied):
    # Simulates the controller crashing after fencing but before killing the old sandbox.
    ctl, zombie = proxied(session="zombie", epoch=1)
    assert fetch(ctl, zombie, GITHUB).stdout.startswith("200 ")
    proxied.registry.bump_epoch(zombie.caller.key, 2)
    assert "403" in fetch(ctl, zombie, GITHUB).stdout
    assert [l for l in lines(AUDIT_LOG) if l["session"] == zombie.caller.session][-1]["reason"] == "stale epoch"


def test_unregistered_caller_is_denied(proxied):
    ctl, sbx = proxied()
    proxied.registry.unregister(sbx.name)
    assert "403" in fetch(ctl, sbx, GITHUB).stdout
    assert lines(AUDIT_LOG)[-1]["reason"] == "unknown caller"


def test_sandboxes_cannot_reach_each_other(proxied):
    ctl, a = proxied(tenant="tenant-a")
    _, b = proxied(agent="agent-b", tenant="tenant-b")
    listener = threading.Thread(target=lambda: py(ctl, a, (
        "import socket\ns=socket.socket(); s.setsockopt(1,2,1); s.bind(('0.0.0.0',9000)); s.listen(); s.settimeout(8)\n"
        "c,_=s.accept(); c.send(b'A-SECRET')"), timeout_s=10))
    listener.start(); time.sleep(1.5)
    ip = subprocess.run(["docker", "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}", a.name],
                        capture_output=True, text=True).stdout.split()[0]
    r = py(ctl, b, f"import socket; print(socket.create_connection(('{ip}', 9000), 4).recv(20))")
    assert "A-SECRET" not in r.stdout and r.exit_code != 0
    assert py(ctl, b, f"import socket; socket.getaddrinfo('{a.name}', 9000)").exit_code != 0   # not even by name
    listener.join()


def test_plaintext_http_and_other_ports_are_not_forwarded(proxied):
    ctl, sbx = proxied()
    before = len(lines(STUB_LOG))
    assert "403" in fetch(ctl, sbx, "http://api.github.test/user").stdout            # plaintext, port 80
    assert "403" in fetch(ctl, sbx, "http://api.github.test:443/user").stdout       # plaintext on a granted port: only the scheme check stops this
    assert "403" in fetch(ctl, sbx, "https://api.github.test:8443/user").stdout     # TLS on a port that is not granted
    assert len(lines(STUB_LOG)) == before


def test_a_policy_error_denies_instead_of_forwarding(proxied):
    ctl, sbx = proxied(agent="agent-broken")      # its grant names a credential that does not exist
    before = len(lines(STUB_LOG))
    assert "HTTPError" in fetch(ctl, sbx, GITHUB).stdout
    assert len(lines(STUB_LOG)) == before         # failed closed: nothing reached the destination
    assert lines(AUDIT_LOG)[-1]["decision"] == "deny"


def test_a_failed_upstream_call_is_still_audited(proxied):
    ctl, sbx = proxied()
    fetch(ctl, sbx, "https://down.test/x")        # granted, but does not resolve
    entry = [l for l in lines(AUDIT_LOG) if l["host"] == "down.test"][-1]
    assert entry["decision"] == "error" and entry["session"] == sbx.caller.session


def test_proxy_rejects_an_agent_tenant_mismatch(proxied):
    ctl, sbx = proxied()
    proxied.registry.register(sbx.ip, Caller(sbx.caller.session, "agent-a", "tenant-b", 1), sbx.name)  # bypasses the controller
    before = len(lines(STUB_LOG))
    assert "403" in fetch(ctl, sbx, GITHUB).stdout
    assert len(lines(STUB_LOG)) == before
    assert lines(AUDIT_LOG)[-1]["reason"] == "agent/tenant mismatch"


def test_controller_refuses_an_agent_tenant_mismatch(proxied, tmp_path):
    import pytest
    from sandbox_controller import SandboxError
    ctl, _ = proxied()
    with pytest.raises(SandboxError, match="mismatch"):
        ctl.create(tmp_path / "m", Caller("s-mm", "agent-a", "tenant-b", 1))


def test_stopping_a_sandbox_removes_its_network(proxied):
    ctl, sbx = proxied()
    nets = lambda: subprocess.run(["docker", "network", "ls", "-q", "--filter", f"name={sbx.network}"],
                                  capture_output=True, text=True).stdout.split()
    assert nets()
    ctl.stop(sbx)
    assert not nets()


def test_a_corrupt_registry_denies_everything(proxied):
    # The proxy's own failure handling must not depend on the thing that failed.
    ctl, sbx = proxied()
    path = proxied.registry.path
    good = path.read_text()
    path.write_text("{ this is not json")
    try:
        before = len(lines(STUB_LOG))
        out = fetch(ctl, sbx, GITHUB).stdout
        assert "HTTPError" in out or "Tunnel connection failed" in out      # denied: no 200
        assert len(lines(STUB_LOG)) == before                               # nothing reached the destination
        assert "Tunnel connection failed: 200" not in out
    finally:
        path.write_text(good)


def test_query_strings_are_left_alone_except_for_the_placeholder(proxied):
    ctl, sbx = proxied()
    before = len(lines(STUB_LOG))
    fetch(ctl, sbx, GITHUB + "?label=a&label=b&q=x%20y")
    fetch(ctl, sbx, GITHUB + "?access_token=placeholder-github-token")
    first, second = [r["path"] for r in lines(STUB_LOG)[before:]]
    assert first == "/user?label=a&label=b&q=x%20y"                  # repeated keys and encoding untouched
    assert second == f"/user?access_token={SECRET}"                  # the placeholder in a query IS swapped


def test_a_comma_in_the_workdir_cannot_inject_mount_options(make_sandbox, tmp_path):
    import pytest
    from sandbox_controller import SandboxError
    with pytest.raises(SandboxError, match="workdir path"):
        make_sandbox(tmp_path / "a,readonly=false")


def test_reap_clears_what_a_crashed_controller_left_behind(proxied, tmp_path):
    from sandbox_controller import SandboxController
    ctl, sbx = proxied()
    SandboxController().reap()          # a fresh controller that knows nothing about the proxy or this sandbox
    labelled = lambda *a: subprocess.run(["docker", *a, "--filter", "label=agent-sandbox=1", "-q"],
                                         capture_output=True, text=True).stdout.split()
    assert not labelled("ps", "-a") and not labelled("network", "ls")


def test_stop_still_kills_the_sandbox_when_the_registry_is_broken(proxied, monkeypatch):
    import pytest
    ctl, sbx = proxied()
    def boom(*a, **kw):
        raise OSError("registry unwritable")
    monkeypatch.setattr(proxied.registry, "unregister", boom)
    with pytest.raises(OSError, match="registry unwritable"):
        ctl.stop(sbx)
    assert subprocess.run(["docker", "inspect", sbx.name], capture_output=True).returncode != 0   # killed regardless
    nets = subprocess.run(["docker", "network", "ls", "-q", "--filter", f"name={sbx.network}"], capture_output=True, text=True)
    assert not nets.stdout.split()


def test_a_second_controller_still_refuses_a_stale_epoch(proxied, tmp_path):
    import pytest
    from sandbox_controller import SandboxController, SandboxError
    ctl, current = proxied(session="two-ctl", epoch=3)
    fresh = SandboxController(proxy=ctl.proxy)          # knows nothing in memory: only the registry remembers epoch 3
    with pytest.raises(SandboxError, match="stale epoch"):
        fresh.create(tmp_path / "z", Caller(current.caller.session, "agent-a", "tenant-a", 2))


def test_reap_also_unregisters_what_it_kills(proxied):
    ctl, sbx = proxied()
    ctl.reap()
    assert proxied.registry.lookup(sbx.ip) is None


def test_a_comma_in_the_ca_path_is_refused(proxied, tmp_path):
    import dataclasses
    import pytest
    from sandbox_controller import SandboxController, SandboxError
    ctl, _ = proxied()
    bad = SandboxController(proxy=dataclasses.replace(ctl.proxy, ca_cert=tmp_path / "x,readonly=false.pem"))
    with pytest.raises(SandboxError, match="ca_cert path"):
        bad.create(tmp_path / "wd", Caller("s-bad-ca", "agent-a", "tenant-a", 1))


def test_dockerignore_keeps_state_out_of_the_build_context():
    # Defence in depth. BuildKit only transfers files a COPY names (measured: 506 bytes with or without
    # this file), but the legacy builder sends the whole directory, including the proxy's CA private key.
    ignore = (STATE.parent / ".dockerignore").read_text().split()
    assert ".state" in ignore and ".work" in ignore


def test_a_second_controller_refuses_an_equal_epoch_while_the_first_sandbox_lives(proxied, tmp_path):
    import pytest
    from sandbox_controller import SandboxController, SandboxError
    ctl, live = proxied(session="equal", epoch=4)
    fresh = SandboxController(proxy=ctl.proxy)
    with pytest.raises(SandboxError, match="live sandbox"):
        fresh.create(tmp_path / "e", Caller(live.caller.session, "agent-a", "tenant-a", 4))


def test_a_newer_epoch_from_another_controller_stops_the_old_sandbox(proxied, tmp_path):
    from sandbox_controller import SandboxController
    ctl, old = proxied(session="takeover", epoch=1)
    fresh = SandboxController(proxy=ctl.proxy)                # no memory of `old`: only the registry knows it
    new = fresh.create(tmp_path / "t", Caller(old.caller.session, "agent-a", "tenant-a", 2))
    try:
        assert subprocess.run(["docker", "inspect", old.name], capture_output=True).returncode != 0
        assert [e for _, e in proxied.registry.sandboxes(old.caller.key)] == [2]
    finally:
        fresh.stop(new)


def test_reap_drops_registry_entries_for_sandboxes_that_already_vanished(proxied):
    ctl, sbx = proxied()
    subprocess.run(["docker", "kill", sbx.name], capture_output=True)       # expired on its own; the registry still lists it
    ctl.reap()
    assert proxied.registry.lookup(sbx.ip) is None


def test_a_relative_ca_path_works(proxied, tmp_path):
    import dataclasses
    import os
    from sandbox_controller import SandboxController
    ctl, _ = proxied()
    rel = os.path.relpath(ctl.proxy.ca_cert)
    other = SandboxController(proxy=dataclasses.replace(ctl.proxy, ca_cert=Path(rel)))
    sbx = other.create(tmp_path / "r", Caller("s-rel-ca-" + uuid.uuid4().hex[:8], "agent-a", "tenant-a", 1))
    other.stop(sbx)


def test_the_registry_directory_is_not_world_writable(stack):
    # It is the proxy's trust root: whoever can replace registry.json decides which caller is which tenant.
    assert not (STATE / "registry").stat().st_mode & 0o002


def test_base_images_are_pinned_by_digest():
    for dockerfile in ("image/Dockerfile", "agent_network_proxy/Dockerfile"):
        assert "@sha256:" in (STATE.parent / dockerfile).read_text(), dockerfile


RAW = """
import socket
s = socket.create_connection(("proxy", 8080), 5)
s.sendall(b"CONNECT api.github.test:443 HTTP/1.1\\r\\nHost: api.github.test:443\\r\\n\\r\\n"); s.recv(200)
s.sendall(b"SSH-2.0-test\\r\\n"); s.settimeout(3)
try:
    print("EOF" if s.recv(200) == b"" else "DATA")
except TimeoutError:
    print("HELD")
"""


def test_non_http_traffic_inside_a_granted_tunnel_is_not_relayed(proxied):
    # With mitmproxy's raw-TCP fallback on, bytes that are not HTTP are passed to the upstream server (it hangs up
    # at once: EOF), and none of it is audited, rate limited or scrubbed. With it off, the proxy refuses to relay.
    ctl, sbx = proxied()
    assert py(ctl, sbx, RAW).stdout.strip() == "HELD"


def test_a_websocket_upgrade_is_denied(proxied):
    ctl, sbx = proxied()
    code = ("import urllib.request as u\n"
            "r = u.Request('https://api.github.test/user', headers={'Connection': 'Upgrade', 'Upgrade': 'websocket'})\n"
            "try:\n    print(u.urlopen(r, timeout=6).status)\nexcept Exception as e:\n    print(repr(e))")
    assert "HTTPError 403" in py(ctl, sbx, code).stdout
    assert lines(AUDIT_LOG)[-1]["reason"] == "upgrade not permitted"


def test_the_proxy_container_is_found_by_its_compose_labels(proxied):
    ctl, _ = proxied()
    label = subprocess.run(["docker", "inspect", "-f", '{{index .Config.Labels "com.docker.compose.service"}}',
                            ctl._proxy_container()], capture_output=True, text=True).stdout.strip()
    assert label == "proxy"          # whatever Compose called it, the container found is the proxy service


def test_malformed_grants_are_a_sandbox_error_not_a_crash(tmp_path):
    import pytest
    from agent_network_proxy.registry import Registry
    from sandbox_controller import ProxyConfig, SandboxController, SandboxError
    for bad in ("{ not json", json.dumps({"agents": {"a": {"tenant": "t"}}})):       # unparsable; missing "destinations"
        grants = tmp_path / "grants.json"
        grants.write_text(bad)
        ctl = SandboxController(proxy=ProxyConfig("http://proxy:8080", STATE / "proxy-conf" / "mitmproxy-ca-cert.pem",
                                                  Registry(tmp_path / "r.json"), grants))
        with pytest.raises(SandboxError, match="grants"):
            ctl.create(tmp_path / "wd", Caller("s", "a", "t", 1))


def test_a_workdir_that_is_not_dedicated_is_refused(make_sandbox, tmp_path):
    import pytest
    from sandbox_controller import SandboxError
    precious = tmp_path / "my-project"
    precious.mkdir()
    (precious / "notes.txt").write_text("not for the sandbox")
    with pytest.raises(SandboxError, match="not dedicated"):
        make_sandbox("my-project")                       # an existing, non-empty directory the controller did not create
    assert not precious.stat().st_mode & 0o002           # and it was not made world-writable
    ctl, sbx = make_sandbox("fresh")
    ctl.stop(sbx)
    make_sandbox("fresh")                                # a directory the controller created can be reused (a resumed session)


def test_a_failed_stop_after_a_timeout_does_not_lose_the_result(proxied, monkeypatch):
    ctl, sbx = proxied()
    real_stop = ctl.stop
    def stop_then_fail(sandbox):
        real_stop(sandbox)
        raise OSError("registry unwritable")
    monkeypatch.setattr(ctl, "stop", stop_then_fail)
    result = ctl.exec(sbx, ["sleep", "30"], timeout_s=1)
    assert result.timed_out                              # the caller still learns the command was cut off


def test_stop_survives_a_broken_docker_client(proxied):
    import pytest
    ctl, sbx = proxied()
    real = ctl.docker
    ctl.docker = "/nonexistent/docker"
    try:
        with pytest.raises(OSError):
            ctl.stop(sbx)
        assert not sbx.stopped
        assert ctl._by_session[sbx.caller.key] is sbx             # still remembered: it may be running, so a fence must see it
    finally:
        ctl.docker = real
        ctl.stop(sbx)                                                # and a retry then succeeds
    assert sbx.stopped and sbx.caller.key not in ctl._by_session


def test_an_unreachable_daemon_is_not_mistaken_for_a_stopped_sandbox(proxied, tmp_path):
    import pytest
    from sandbox_controller import SandboxError
    ctl, sbx = proxied()
    broken = tmp_path / "docker"                            # a docker CLI that cannot reach its daemon
    broken.write_text("#!/bin/sh\necho 'Cannot connect to the Docker daemon at unix:///var/run/docker.sock' >&2\nexit 1\n")
    broken.chmod(0o755)
    real = ctl.docker
    ctl.docker = str(broken)
    try:
        with pytest.raises(SandboxError, match="cannot tell"):
            ctl._wait_until_gone(sbx.name)
        with pytest.raises(Exception):
            ctl.stop(sbx)
        assert sbx.stopped is False                         # still running as far as anyone knows: retry later
    finally:
        ctl.docker = real
        ctl.stop(sbx)
    assert sbx.stopped


def test_a_create_that_will_be_rejected_does_not_disturb_the_live_sandbox(proxied, tmp_path):
    import pytest
    from sandbox_controller import SandboxError
    ctl, live = proxied(session="validate", epoch=1)
    precious = tmp_path / "project"
    precious.mkdir()
    (precious / "file.txt").write_text("x")
    with pytest.raises(SandboxError, match="not dedicated"):
        ctl.create(precious, Caller(live.caller.session, "agent-a", "tenant-a", 2))
    assert live.alive and subprocess.run(["docker", "inspect", live.name], capture_output=True).returncode == 0
    assert proxied.registry.newest_epoch(live.caller.key) == 1          # and the epoch was not raised


def test_session_ids_are_scoped_to_their_tenant(proxied, tmp_path):
    ctl, a = proxied(session="shared-id", epoch=1)
    b = ctl.create(tmp_path / "b", Caller(a.caller.session, "agent-b", "tenant-b", 2))     # same id, other tenant
    try:
        assert a.alive and subprocess.run(["docker", "inspect", a.name], capture_output=True).returncode == 0
        assert proxied.registry.newest_epoch(a.caller.key) == 1
    finally:
        ctl.stop(b)


def test_a_missing_image_fails_fast_instead_of_pulling(tmp_path):
    import pytest
    from sandbox_controller import SandboxController, SandboxError
    # A missing local image must be "No such image", not an attempt to pull the name from a public registry
    # ("pull access denied"): the sandbox image is built locally and must never be fetched from elsewhere.
    with pytest.raises(SandboxError, match="No such image") as err:
        SandboxController(image="agent-sandbox:does-not-exist").create(tmp_path / "w")
    assert "pull access" not in str(err.value)


def test_two_controllers_racing_for_one_session_cannot_both_win(proxied, tmp_path):
    import threading
    from sandbox_controller import SandboxController, SandboxError
    ctl, _ = proxied()
    caller = Caller("race-" + uuid.uuid4().hex[:8], "agent-a", "tenant-a", 2)
    gate, outcomes = threading.Barrier(2), []
    def contend(i):
        c = SandboxController(proxy=ctl.proxy)        # two controllers that know nothing of each other
        gate.wait()
        try:
            outcomes.append(c.create(tmp_path / f"race{i}", caller))
        except SandboxError as exc:
            outcomes.append(exc)
    threads = [threading.Thread(target=contend, args=(i,)) for i in range(2)]
    [t.start() for t in threads]; [t.join() for t in threads]
    winners = [o for o in outcomes if not isinstance(o, Exception)]
    try:
        assert len(winners) == 1, outcomes
        assert len(proxied.registry.sandboxes(caller.key)) == 1
    finally:
        for w in winners:
            ctl.stop(w)


def test_a_create_that_fails_and_cannot_clean_up_says_what_it_left_behind(proxied, tmp_path, monkeypatch):
    import pytest
    from sandbox_controller import SandboxError
    ctl, _ = proxied()
    def no_register(*a, **kw):
        raise OSError("registry unwritable")
    monkeypatch.setattr(ctl.proxy.registry, "register", no_register)
    monkeypatch.setattr(ctl, "_remove_network", lambda n: (_ for _ in ()).throw(OSError("daemon busy")))
    with pytest.raises(SandboxError, match=r"sbx-\w+.*reap") as err:
        ctl.create(tmp_path / "orphan", Caller("s-orphan-" + uuid.uuid4().hex[:8], "agent-a", "tenant-a", 1))
    assert "registry unwritable" in str(err.value) and "daemon busy" in str(err.value)
    monkeypatch.undo()
    ctl.reap()                                           # and reap() really does clean it up


def test_the_same_epoch_can_be_retried_after_its_sandbox_expired(tmp_path):
    from sandbox_controller import Limits, SandboxController
    ctl = SandboxController()                            # no proxy: only the in-memory map knows the old sandbox
    caller = Caller("expire-" + uuid.uuid4().hex[:8], "agent-no-net", "tenant-a", 1)
    ctl.create(tmp_path / "w", caller, Limits(deadline_s=1))
    time.sleep(3)                                        # it exited at its deadline, but nobody called stop()
    second = ctl.create(tmp_path / "w", caller)
    ctl.stop(second)


def test_a_workdir_belongs_to_one_session(make_sandbox, tmp_path):
    import pytest
    from sandbox_controller import SandboxError
    ctl, mine = make_sandbox("shared", caller=Caller("s-a", "agent-no-net", "tenant-a", 1))
    ctl.stop(mine)
    with pytest.raises(SandboxError, match="belongs to"):                          # another tenant, same directory
        ctl.create(mine.workdir, Caller("s-a", "agent-b", "tenant-b", 1))
    victim = tmp_path / "host-file"
    victim.write_text("x")
    owner = mine.workdir.with_name(mine.workdir.name + ".sandbox-owner")
    owner.unlink()
    owner.symlink_to(victim)                                                       # a marker that is a symlink is never followed
    before = victim.stat().st_mtime_ns
    with pytest.raises(SandboxError, match="not dedicated"):
        ctl.create(mine.workdir, Caller("s-a", "agent-no-net", "tenant-a", 1))
    assert victim.stat().st_mtime_ns == before


def test_basic_auth_placeholders_are_swapped_too(proxied):
    import base64
    ctl, sbx = proxied()
    before = len(lines(STUB_LOG))
    basic = base64.b64encode(b"x-access-token:placeholder-github-token").decode()
    py(ctl, sbx, "import urllib.request as u\n"
                 f"u.urlopen(u.Request('{GITHUB}', headers={{'Authorization': 'Basic {basic}'}}), timeout=8).read()")
    got = lines(STUB_LOG)[before]["headers"]["Authorization"]
    assert base64.b64decode(got.split()[1]).decode() == f"x-access-token:{SECRET}"


def test_a_placeholder_where_it_cannot_be_swapped_is_refused_not_forwarded(proxied):
    ctl, sbx = proxied()
    before = len(lines(STUB_LOG))
    in_path = py(ctl, sbx, "import urllib.request as u\n"
                           "try: print(u.urlopen('https://api.github.test/bot-placeholder-github-token/send', timeout=8).status)\n"
                           "except Exception as e: print(repr(e))").stdout
    in_body = py(ctl, sbx, "import urllib.request as u\n"
                           "r = u.Request('https://api.github.test/user', data=b'{\"token\": \"placeholder-github-token\"}', method='POST')\n"
                           "try: print(u.urlopen(r, timeout=8).status)\n"
                           "except Exception as e: print(repr(e))").stdout
    assert "HTTPError 403" in in_path and "HTTPError 403" in in_body
    assert len(lines(STUB_LOG)) == before
    assert lines(AUDIT_LOG)[-1]["reason"] == "placeholder in an unsupported position"


def test_a_controller_that_loses_the_fence_race_does_not_keep_its_sandbox(proxied, tmp_path):
    import pytest
    from sandbox_controller import SandboxController, SandboxError
    ctl, _ = proxied()
    x, y = SandboxController(proxy=ctl.proxy), SandboxController(proxy=ctl.proxy)
    session = "lose-" + uuid.uuid4().hex[:8]
    older, newer = Caller(session, "agent-a", "tenant-a", 1), Caller(session, "agent-a", "tenant-a", 2)
    won, real = {}, x._docker
    def intercept(args, what):
        out = real(args, what)
        if what == "inspect sandbox":                     # x's container is running but not yet registered
            won["y"] = y.create(tmp_path / "y", newer)    # a newer executor takes over right now
        return out
    x._docker = intercept
    try:
        with pytest.raises(SandboxError) as err:
            x.create(tmp_path / "x", older)
        assert "took over" in str(err.value), err.value          # lost the race, not some unrelated failure
        live = [name for name, _ in proxied.registry.sandboxes(newer.key)]
        assert live == [won["y"].name]                    # exactly one sandbox holds the session: the newer one
        docker_names = subprocess.run(["docker", "ps", "--filter", "label=agent-sandbox=1", "--format", "{{.Names}}"],
                                      capture_output=True, text=True).stdout.split()
        assert won["y"].name in docker_names and len([n for n in docker_names if n.startswith("sbx-")]) == 2   # y's and the fixture's
    finally:
        if "y" in won:
            y.stop(won["y"])


def test_a_failed_fence_does_not_leave_its_claim_behind(proxied, tmp_path, monkeypatch):
    import pytest
    from sandbox_controller import SandboxController
    ctl, old = proxied(session="claim", epoch=1)
    fresh = SandboxController(proxy=ctl.proxy)
    caller = Caller(old.caller.session, "agent-a", "tenant-a", 2)
    real_stop = fresh.stop
    monkeypatch.setattr(fresh, "stop", lambda sandbox: (_ for _ in ()).throw(OSError("daemon hiccup")))
    with pytest.raises(OSError):
        fresh.create(tmp_path / "c1", caller)
    assert not any(n.startswith("sbx-") and e == 2 for n, e in proxied.registry.sandboxes(caller.key))    # no claim left
    monkeypatch.setattr(fresh, "stop", real_stop)
    again = fresh.create(tmp_path / "c2", caller)          # the same epoch can retry at once, not after a 60 s expiry
    fresh.stop(again)


def test_an_echoed_basic_credential_is_scrubbed_too(proxied):
    import base64
    ctl, sbx = proxied()
    basic = base64.b64encode(b"x-access-token:placeholder-github-token").decode()
    out = py(ctl, sbx, "import urllib.request as u\n"
                       f"r = u.urlopen(u.Request('https://api.github.test/headers', headers={{'Authorization': 'Basic {basic}'}}), timeout=8)\n"
                       "print(r.headers.get('X-Echo')); print(r.read())").stdout
    leaked = base64.b64encode(f"x-access-token:{SECRET}".encode()).decode()
    assert leaked not in out and "[REDACTED]" in out


def test_a_placeholder_in_a_query_parameter_name_is_refused(proxied):
    ctl, sbx = proxied()
    before = len(lines(STUB_LOG))
    assert "HTTPError 403" in fetch(ctl, sbx, GITHUB + "?placeholder-github-token=1").stdout
    assert len(lines(STUB_LOG)) == before


def test_a_hung_docker_inspect_is_an_error_not_a_hang(proxied, tmp_path):
    import pytest
    from sandbox_controller import SandboxError
    ctl, sbx = proxied()
    slow = tmp_path / "docker"
    slow.write_text("#!/bin/sh\nsleep 30\n")
    slow.chmod(0o755)
    real = ctl.docker
    ctl.docker, ctl.inspect_timeout_s = str(slow), 1
    started = time.monotonic()
    try:
        with pytest.raises(SandboxError, match="cannot tell"):
            ctl._exists(sbx.name)            # runs while the registry lock is held, so it must be bounded
        assert time.monotonic() - started < 5
    finally:
        ctl.docker = real


def test_tenant_ids_cannot_collide_through_a_slash():
    import pytest
    assert Caller("c", "a", "a", 1).key != Caller("b/c", "a", "a", 1).key
    with pytest.raises(ValueError, match="tenant"):
        Caller("c", "agent", "a/b", 1)


def test_reap_distrusts_every_address_even_if_the_daemon_is_down(proxied):
    import pytest
    ctl, sbx = proxied()
    real = ctl.docker
    ctl.docker = "/nonexistent/docker"
    try:
        with pytest.raises(OSError):
            ctl.reap()
        assert proxied.registry.lookup(sbx.ip) is None        # cleared first: the proxy stops trusting it regardless
    finally:
        ctl.docker = real
        ctl.reap()


def test_the_ownership_marker_is_outside_the_sandbox(make_sandbox):
    # Whatever runs in /work must not be able to strand its own session by deleting the marker.
    ctl, sbx = make_sandbox("owned", caller=Caller("s-own-" + uuid.uuid4().hex[:6], "agent-no-net", "tenant-a", 1))
    assert not (sbx.workdir / ".agent-sandbox-workdir").exists()
    ctl.exec(sbx, ["bash", "-c", "echo data > /work/keep.txt; rm -rf /work/.[!.]* /work/*.marker 2>/dev/null; echo data > /work/keep.txt"])
    ctl.stop(sbx)
    resumed = ctl.create(sbx.workdir, sbx.caller)            # the session can resume: the marker was not in reach
    ctl.stop(resumed)
