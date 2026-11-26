import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent_network_proxy.registry import Caller, Registry  # noqa: E402
from sandbox_controller import ProxyConfig, SandboxController  # noqa: E402

STATE = ROOT / ".state"
GRANTS = ROOT / "config" / "grants.json"


def _require_image(name: str) -> None:
    if subprocess.run(["docker", "image", "inspect", name], capture_output=True).returncode:
        pytest.fail(f"image {name} missing: run `make image`", pytrace=False)


@pytest.fixture(scope="session")
def stack():
    """The proxy + stub + networks (docker-compose.yml). Started once, left running for inspection."""
    import stack as stack_mod
    stack_mod.up()
    return stack_mod


@pytest.fixture(scope="session", autouse=True)
def no_leaked_sandboxes():
    """After the whole run, no sandbox container or sandbox network may be left behind."""
    yield
    docker = lambda *a: subprocess.run(["docker", *a], capture_output=True, text=True).stdout.split()
    leaked = docker("ps", "-aq", "--filter", "label=agent-sandbox=1") + \
        docker("network", "ls", "-q", "--filter", "label=agent-sandbox=1")
    if leaked:
        SandboxController(proxy=None).reap()
        pytest.fail(f"leaked sandbox resources: {leaked}", pytrace=False)


@pytest.fixture
def make_sandbox(tmp_path):
    """Factory for plain (no network) sandboxes; everything created is killed afterwards."""
    _require_image("agent-sandbox:poc")
    ctl = SandboxController()
    made = []

    def make(name="work", **kw):
        sbx = ctl.create(tmp_path / name, **kw)
        made.append(sbx)
        return ctl, sbx
    yield make
    for sbx in made:
        ctl.stop(sbx)


@pytest.fixture
def proxied(stack, tmp_path):
    """Factory for sandboxes on the internal network, registered with the proxy."""
    _require_image("agent-sandbox:poc")
    registry = Registry(STATE / "registry" / "registry.json")
    ctl = SandboxController(proxy=ProxyConfig(
        proxy_url="http://proxy:8080", ca_cert=STATE / "proxy-conf" / "mitmproxy-ca-cert.pem",
        registry=registry, grants_path=GRANTS))
    made = []
    run_id = uuid.uuid4().hex[:8]   # the registry remembers epochs across runs, so session ids must be unique

    def make(agent="agent-a", tenant="tenant-a", session=None, epoch=1):
        session = f"{session or f's{len(made)}'}-{run_id}"
        sbx = ctl.create(tmp_path / session, Caller(session, agent, tenant, epoch))
        made.append(sbx)
        return ctl, sbx
    make.registry = registry
    yield make
    for sbx in made:
        ctl.stop(sbx)


def sh(ctl, sbx, code, **kw):
    return ctl.exec(sbx, ["bash", "-c", code], **kw)


def py(ctl, sbx, code, **kw):
    return ctl.exec(sbx, ["python3", "-c", code], **kw)


STUB_LOG = STATE / "stub" / "received.jsonl"
AUDIT_LOG = STATE / "audit" / "audit.jsonl"

# A sandbox-side HTTP client. argv[1]=url. Prints status and body, or the error.
FETCH = '''
import sys, urllib.request
req = urllib.request.Request(sys.argv[1], headers={"Authorization": "Bearer placeholder-github-token"})
try:
    r = urllib.request.urlopen(req, timeout=8)
    print(r.status, r.read().decode())
except Exception as e:
    print("ERROR", repr(e))
'''


def fetch(ctl, sbx, url):
    return ctl.exec(sbx, ["python3", "-c", FETCH, url])


def lines(path):
    return [__import__("json").loads(l) for l in path.read_text().splitlines()] if path.exists() else []
