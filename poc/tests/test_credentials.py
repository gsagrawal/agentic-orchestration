"""Credentials: code can *use* a credential without being able to *read* it."""
import json
import re
import time

from agent_network_proxy.registry import Caller
from conftest import AUDIT_LOG, STATE, STUB_LOG, fetch, lines, py, sh

SECRETS = json.loads((STATE.parent / "config" / "secrets.json").read_text())
REAL_A = SECRETS["tenant-a"]["github"]
GITHUB = "https://api.github.test/user"


def test_stub_receives_the_real_token_but_the_sandbox_never_sees_it(proxied):
    ctl, sbx = proxied()
    before = len(lines(STUB_LOG))
    out = fetch(ctl, sbx, GITHUB).stdout
    received = lines(STUB_LOG)[before:]
    assert [r["headers"]["Authorization"] for r in received] == [f"Bearer {REAL_A}"]   # the credential worked
    assert REAL_A not in out and "[REDACTED]" in out     # the stub echoed it back; the proxy scrubbed it


def test_the_sandbox_holds_only_the_placeholder(proxied):
    ctl, sbx = proxied()
    r = sh(ctl, sbx, 'echo "$GH_TOKEN"')
    assert r.stdout.strip() == "placeholder-github-token"


def leaked_paths(ctl, sbx, needle: str) -> list[str]:
    """Paths of every file in the sandbox containing `needle`. Text and binary files are both searched
    (-a), and /proc, /sys and /dev are skipped (/proc/*/environ is checked separately)."""
    r = sh(ctl, sbx, f"grep -rlaF -e '{needle}' / --exclude-dir=proc --exclude-dir=sys --exclude-dir=dev 2>/dev/null")
    return r.stdout.split()


def test_the_leak_detector_detects_a_planted_leak(proxied):
    # Guards the two tests below against passing vacuously: prove the search would find a leak.
    ctl, sbx = proxied()
    sh(ctl, sbx, "echo CANARY-123456 > /tmp/planted; echo CANARY-123456 > /work/planted")
    assert sorted(leaked_paths(ctl, sbx, "CANARY-123456")) == ["/tmp/planted", "/work/planted"]


def test_real_token_is_nowhere_in_the_sandbox(proxied):
    ctl, sbx = proxied()
    fetch(ctl, sbx, GITHUB)                              # make sure a credentialed call has happened
    unique = REAL_A.split("FAKE_")[1]                    # also catch the token minus its prefix
    assert leaked_paths(ctl, sbx, REAL_A) == [] and leaked_paths(ctl, sbx, unique) == []
    env = sh(ctl, sbx, "env; cat /proc/*/environ 2>/dev/null | tr '\\0' '\\n'").stdout
    assert unique not in env and "placeholder-github-token" in env     # the env check itself works


def test_proxy_private_key_is_not_in_the_sandbox(proxied):
    ctl, sbx = proxied()
    pem = (STATE / "proxy-conf" / "mitmproxy-ca.pem").read_text()
    key_line = next(l for l in re.split(r"-----BEGIN [A-Z ]*PRIVATE KEY-----", pem)[1].splitlines() if len(l) > 40)
    cert_line = next(l for l in (STATE / "proxy-conf" / "mitmproxy-ca-cert.pem").read_text().splitlines() if len(l) > 40)
    assert leaked_paths(ctl, sbx, cert_line) == ["/etc/ssl/proxy-ca.pem"]     # control: the public cert IS installed
    assert leaked_paths(ctl, sbx, key_line) == []                              # the private key is not


def test_each_tenant_gets_its_own_credential(proxied):
    ctl, sbx = proxied(agent="agent-b", tenant="tenant-b")
    before = len(lines(STUB_LOG))
    fetch(ctl, sbx, GITHUB)
    got = lines(STUB_LOG)[before]["headers"]["Authorization"]
    assert got == f"Bearer {SECRETS['tenant-b']['github']}" and REAL_A not in got


def test_audit_names_the_credential_and_never_holds_a_value(proxied):
    ctl, sbx = proxied()
    fetch(ctl, sbx, GITHUB)
    entry = [l for l in lines(AUDIT_LOG) if l["session"] == sbx.caller.session and l["decision"] == "allow"][-1]
    assert entry["credential"] == "github" and entry["tenant"] == "tenant-a"
    raw = AUDIT_LOG.read_text()
    assert not any(v in raw for t in SECRETS.values() for v in t.values() if v)      # `if v`: one tenant's secret is deliberately empty


def test_a_tool_that_pins_the_certificate_fails_closed_with_no_credential(proxied):
    # The proxy re-signs TLS with its own CA, so a client pinned to the real cert refuses the
    # connection before it sends a single byte. No credential is injected, none is leaked.
    import hashlib, ssl
    der = ssl.PEM_cert_to_DER_cert((STATE / "stub" / "cert.pem").read_text())
    pin = hashlib.sha256(der).hexdigest()
    pinned_client = f'''
import hashlib, socket, ssl
s = socket.create_connection(("proxy", 8080), 5)
s.sendall(b"CONNECT api.github.test:443 HTTP/1.1\\r\\nHost: api.github.test:443\\r\\n\\r\\n"); s.recv(4096)
t = ssl.create_default_context().wrap_socket(s, server_hostname="api.github.test")
seen = hashlib.sha256(t.getpeercert(True)).hexdigest()
if seen != "{pin}":
    raise SystemExit("PIN MISMATCH")
t.sendall(b"GET /user HTTP/1.1\\r\\nHost: api.github.test\\r\\nAuthorization: Bearer placeholder-github-token\\r\\n\\r\\n")
'''
    ctl, sbx = proxied()
    before = len(lines(STUB_LOG))
    r = py(ctl, sbx, pinned_client)
    assert r.exit_code != 0 and "PIN MISMATCH" in r.stderr
    assert len(lines(STUB_LOG)) == before


def test_response_scrubbing_keeps_headers_intact(proxied):
    ctl, sbx = proxied()
    code = (
        "import urllib.request as u\n"
        "r = u.urlopen(u.Request('https://api.github.test/headers', headers={'Authorization': 'Bearer placeholder-github-token'}), timeout=8)\n"
        "print(r.headers.get_all('Set-Cookie')); print(r.headers.get('X-Echo')); print(r.read())\n"
        "print(u.urlopen(u.Request('https://api.github.test/headers', method='HEAD'), timeout=8).headers.get('Content-Length'))")
    out = py(ctl, sbx, code).stdout.splitlines()
    assert out == ["['a=1', 'b=2']", "Bearer [REDACTED]", "b'hello'", "5"]   # both cookies, echo scrubbed, HEAD length kept


def test_one_env_var_cannot_stand_for_two_different_placeholders(tmp_path):
    import pytest
    from agent_network_proxy.registry import Registry
    from sandbox_controller import ProxyConfig, SandboxController, SandboxError
    grants = tmp_path / "grants.json"
    d = lambda ph: {"credential": "c", "env_var": "GH_TOKEN", "placeholder": ph}
    grants.write_text(json.dumps({"agents": {"a": {"tenant": "t", "tools": ["bash"],
                                                    "destinations": {"x.test": d("ph-one"), "y.test": d("ph-two")}}}}))
    ctl = SandboxController(proxy=ProxyConfig("http://proxy:8080", STATE / "proxy-conf" / "mitmproxy-ca-cert.pem",
                                              Registry(tmp_path / "reg.json"), grants))
    with pytest.raises(SandboxError, match="GH_TOKEN"):
        ctl.create(tmp_path / "wd", Caller("s", "a", "t", 1))


def test_encoded_echoes_of_the_credential_are_scrubbed(proxied):
    import base64
    ctl, sbx = proxied()
    fetch_encoded = ("import urllib.request as u, sys\n"
                     "print(u.urlopen(u.Request('https://api.github.test/encoded', headers={'Authorization': sys.argv[1]}), timeout=8).read().decode())")
    bearer = ctl.exec(sbx, ["python3", "-c", fetch_encoded, "Bearer placeholder-github-token"]).stdout
    basic_blob = base64.b64encode(b"x-access-token:placeholder-github-token").decode()
    basic = ctl.exec(sbx, ["python3", "-c", fetch_encoded, f"Basic {basic_blob}"]).stdout
    real_b64 = base64.b64encode(REAL_A.encode()).decode()                              # the secret, base64-encoded
    real_blob = base64.b64encode(f"x-access-token:{REAL_A}".encode()).decode()         # the Basic credential without its scheme
    assert REAL_A not in bearer and real_b64 not in bearer
    assert real_blob not in basic and REAL_A not in basic
    # Not covered, by design: a destination that reflects a credential through a further transformation
    # (e.g. base64 of the Basic blob). Scrubbing is defence in depth for a trusted destination, not a guarantee.


def test_an_empty_secret_denies_instead_of_corrupting_responses(proxied):
    ctl, sbx = proxied(agent="agent-empty-secret", tenant="tenant-e")
    before = len(lines(STUB_LOG))
    assert "HTTPError 500" in fetch(ctl, sbx, GITHUB).stdout
    assert len(lines(STUB_LOG)) == before
    assert lines(AUDIT_LOG)[-1]["reason"] == "internal error"


def test_a_response_the_proxy_cannot_decode_is_not_passed_through(proxied):
    # If the body cannot be decoded, it cannot be scrubbed; after a credential swap that must fail closed.
    ctl, sbx = proxied()
    out = ctl.exec(sbx, ["python3", "-c", "import urllib.request as u\n"
                         "try: print(u.urlopen(u.Request('https://api.github.test/weird', headers={'Authorization': 'Bearer placeholder-github-token'}), timeout=8).read())\n"
                         "except Exception as e: print(repr(e))"]).stdout
    assert REAL_A not in out and "HTTPError 500" in out


def test_the_stub_can_restart_without_breaking_the_proxy(proxied):
    import subprocess as sp
    ctl, sbx = proxied()
    assert fetch(ctl, sbx, GITHUB).stdout.startswith("200 ")
    name = sp.run(["docker", "ps", "--filter", "label=com.docker.compose.service=stub", "--format", "{{.Names}}"],
                  capture_output=True, text=True).stdout.split()[0]
    sp.run(["docker", "restart", name], check=True, capture_output=True)
    for _ in range(30):                                  # wait until it is listening again
        if sp.run(["docker", "inspect", "-f", "{{.State.Health.Status}}", name], capture_output=True, text=True).stdout.strip() == "healthy":
            break
        time.sleep(0.5)
    assert fetch(ctl, sbx, GITHUB).stdout.startswith("200 ")      # the proxy still trusts the restarted stub
