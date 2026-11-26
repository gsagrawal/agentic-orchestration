"""Network: the sandbox reaches only the proxy, and the proxy enforces who, where, and how often."""
import json
import subprocess
import threading
import time

from conftest import AUDIT_LOG, STATE, STUB_LOG, fetch, lines, py
from agent_network_proxy.registry import Caller

GITHUB = "https://api.github.test/user"
SECRET = json.loads((STATE.parent / "config" / "secrets.json").read_text())["tenant-a"]["github"]


def _stub_ip():
    fmt = '{{(index .NetworkSettings.Networks "agentpoc_upstream").IPAddress}}'
    return subprocess.run(["docker", "inspect", "-f", fmt, "agentpoc-stub-1"],
                          capture_output=True, text=True, check=True).stdout.strip()


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
    proxied.registry.bump_epoch(zombie.caller.session, 2)
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
