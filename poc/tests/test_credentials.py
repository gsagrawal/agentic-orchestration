"""Credentials: code can *use* a credential without being able to *read* it."""
import json

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


def test_real_token_is_nowhere_in_the_sandbox(proxied):
    ctl, sbx = proxied()
    fetch(ctl, sbx, GITHUB)                              # make sure a credentialed call has happened
    needle = REAL_A.split("_")[-1]                       # also catch a fragment of it
    r = sh(ctl, sbx, f"env; cat /proc/*/environ 2>/dev/null | tr '\\0' '\\n'; "
                     f"grep -rIl -e {needle} / --exclude-dir=proc --exclude-dir=sys --exclude-dir=dev 2>/dev/null; "
                     f"ls -la /etc/ssl/proxy-ca.pem; echo done")
    assert "done" in r.stdout
    assert needle not in r.stdout and REAL_A not in r.stderr


def test_proxy_private_key_is_not_in_the_sandbox(proxied):
    ctl, sbx = proxied()
    pem = (STATE / "proxy-conf" / "mitmproxy-ca.pem").read_text()
    key_body = next(l for l in pem.splitlines() if l and not l.startswith("-----"))
    r = sh(ctl, sbx, f"grep -rIl -e '{key_body}' / --exclude-dir=proc --exclude-dir=sys 2>/dev/null; "
                     "grep -c 'PRIVATE KEY' /etc/ssl/proxy-ca.pem")
    assert key_body not in r.stdout and r.stdout.strip().splitlines()[-1] == "0"   # only the public cert is installed


def test_each_tenant_gets_its_own_credential(proxied):
    ctl, sbx = proxied(agent="agent-b", tenant="tenant-b")
    before = len(lines(STUB_LOG))
    fetch(ctl, sbx, GITHUB)
    got = lines(STUB_LOG)[before]["headers"]["Authorization"]
    assert got == f"Bearer {SECRETS['tenant-b']['github']}" and REAL_A not in got


def test_audit_names_the_credential_and_never_holds_a_value(proxied):
    ctl, sbx = proxied()
    fetch(ctl, sbx, GITHUB)
    entry = [l for l in lines(AUDIT_LOG) if l["session"] == sbx.caller.session][-1]
    assert entry["credential"] == "github" and entry["tenant"] == "tenant-a"
    raw = AUDIT_LOG.read_text()
    assert not any(v in raw for t in SECRETS.values() for v in t.values())


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
