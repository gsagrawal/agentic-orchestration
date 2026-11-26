"""sandbox-controller: creates, limits, and stops sandboxes.

The only component that talks to Docker. It never sees a real secret: it
installs a placeholder token and the proxy's *public* certificate, and
registers the sandbox with the proxy. Shells out to the docker CLI so there
is no third-party dependency. Every docker flag is explained in
docs/docker-flags.md.
"""
from __future__ import annotations

import json
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from agent_network_proxy.registry import Caller, Registry

INTERNAL_NET = "agentpoc_internal"   # `internal: true` in docker-compose.yml: no route out


# All defaults are ASSUMPTIONS, not measurements. See docs/design-choices.md.
@dataclass(frozen=True)
class Limits:
    cpus: float = 1.0
    memory_mb: int = 256
    pids: int = 64
    tmpfs_mb: int = 64
    deadline_s: int = 300           # sandbox lifetime: the container itself exits, killing everything in it
    max_output_bytes: int = 1 << 20


@dataclass(frozen=True)
class ProxyConfig:
    proxy_url: str                  # as seen from inside the internal network
    ca_cert: Path                   # the proxy's PUBLIC certificate (never its key)
    registry: Registry
    grants_path: Path               # to learn which placeholder tokens an agent gets


@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    duration_s: float
    truncated: bool = False


class SandboxError(RuntimeError):
    pass


@dataclass
class Sandbox:
    name: str
    workdir: Path
    limits: Limits
    caller: Caller | None = None
    alive: bool = True


class SandboxController:
    def __init__(self, image: str = "agent-sandbox:poc", proxy: ProxyConfig | None = None,
                 docker: str = "docker"):
        self.image, self.proxy, self.docker = image, proxy, docker
        self._by_session: dict[str, Sandbox] = {}

    # -- lifecycle ---------------------------------------------------------

    def create(self, workdir: Path, caller: Caller | None = None, limits: Limits = Limits(),
               user: str = "65534:65534") -> Sandbox:
        if self.proxy and caller is None:
            raise SandboxError("a proxied sandbox needs a caller identity to register")
        if caller:
            self._fence(caller)
        workdir = Path(workdir).resolve()
        workdir.mkdir(parents=True, exist_ok=True)
        workdir.chmod(0o777)  # the unprivileged sandbox user must be able to write here
        name = f"sbx-{uuid.uuid4().hex[:12]}"
        args = [
            "run", "-d", "--rm", "--name", name, "--label", "agent-sandbox=1",
            "--read-only",
            "--tmpfs", f"/tmp:rw,noexec,nosuid,nodev,size={limits.tmpfs_mb}m",
            "--mount", f"type=bind,src={workdir},dst=/work",
            "--workdir", "/work",
            "--user", user,
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--cpus", str(limits.cpus),
            "--memory", f"{limits.memory_mb}m", "--memory-swap", f"{limits.memory_mb}m",
            "--pids-limit", str(limits.pids),
            "--init",
            "--env", "HOME=/tmp", "--env", "PYTHONDONTWRITEBYTECODE=1",
            *self._network_args(caller),
            self.image, "sleep", str(limits.deadline_s),
        ]
        self._docker(args, "create sandbox")
        sandbox = Sandbox(name=name, workdir=workdir, limits=limits, caller=caller)
        if caller:
            try:
                ip = self._docker(["inspect", "-f",
                                   f'{{{{(index .NetworkSettings.Networks "{INTERNAL_NET}").IPAddress}}}}',
                                   name], "inspect sandbox")
                self.proxy.registry.register(ip, caller, name)
            except Exception:
                self.stop(sandbox)
                raise
            self._by_session[caller.session] = sandbox
        return sandbox

    def stop(self, sandbox: Sandbox) -> None:
        sandbox.alive = False
        if self.proxy:
            self.proxy.registry.unregister(sandbox.name)  # unregister first: no window where a dying sandbox is still trusted
        subprocess.run([self.docker, "kill", sandbox.name], capture_output=True)
        if sandbox.caller and self._by_session.get(sandbox.caller.session) is sandbox:
            del self._by_session[sandbox.caller.session]

    def _fence(self, caller: Caller) -> None:
        """Starting a sandbox with a newer epoch stops the old one; an older or equal epoch is refused."""
        old = self._by_session.get(caller.session)
        if old and caller.epoch <= old.caller.epoch:
            raise SandboxError(f"stale epoch {caller.epoch} for session {caller.session} "
                               f"(current {old.caller.epoch})")
        if self.proxy:
            self.proxy.registry.bump_epoch(caller.session, caller.epoch)  # fence at the proxy first
        if old:
            self.stop(old)

    def _network_args(self, caller: Caller | None) -> list[str]:
        if self.proxy is None:
            return ["--network", "none"]
        grants = json.loads(self.proxy.grants_path.read_text())["agents"][caller.agent]
        env = {d["env_var"]: d["placeholder"] for d in grants["destinations"].values()}
        ca = "/etc/ssl/proxy-ca.pem"
        env.update({k: ca for k in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
                                    "NODE_EXTRA_CA_CERTS", "GIT_SSL_CAINFO")})
        env.update({k: self.proxy.proxy_url for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")})
        args = ["--network", INTERNAL_NET,
                "--mount", f"type=bind,src={self.proxy.ca_cert},dst={ca},readonly"]
        for k, v in env.items():
            args += ["--env", f"{k}={v}"]
        return args

    # -- execution ---------------------------------------------------------

    def exec(self, sandbox: Sandbox, argv: list[str], timeout_s: float = 30,
             stdin: str | None = None) -> ExecResult:
        """Run argv in the sandbox. On timeout the whole sandbox is killed:
        killing only the `docker exec` client would leave the process running."""
        if not sandbox.alive:
            raise SandboxError(f"{sandbox.name} is no longer running")
        cap = sandbox.limits.max_output_bytes
        start = time.monotonic()
        proc = subprocess.Popen([self.docker, "exec", "-i", sandbox.name, *argv],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = _Capture(proc.stdout, cap), _Capture(proc.stderr, cap)
        try:
            proc.stdin.write((stdin or "").encode())
            proc.stdin.close()
        except BrokenPipeError:
            pass
        timed_out = False
        try:
            proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            self.stop(sandbox)
            proc.kill()
            proc.wait()
        out.join(), err.join()
        return ExecResult(exit_code=proc.returncode, stdout=out.text(), stderr=err.text(),
                          timed_out=timed_out, duration_s=time.monotonic() - start,
                          truncated=out.truncated or err.truncated)

    def _docker(self, args: list[str], what: str) -> str:
        done = subprocess.run([self.docker, *args], capture_output=True, text=True)
        if done.returncode != 0:
            raise SandboxError(f"{what} failed: {done.stderr.strip()}")
        return done.stdout.strip()


class _Capture(threading.Thread):
    """Drains a pipe, keeping at most `cap` bytes, so a runaway writer can
    neither block on a full pipe nor exhaust host memory."""

    def __init__(self, stream, cap: int):
        super().__init__(daemon=True)
        self._stream, self._cap = stream, cap
        self._buf = bytearray()
        self.truncated = False
        self.start()

    def run(self) -> None:
        for chunk in iter(lambda: self._stream.read1(65536), b""):
            room = self._cap - len(self._buf)
            if room > 0:
                self._buf += chunk[:room]
            if len(chunk) > room:
                self.truncated = True

    def text(self) -> str:
        return self._buf.decode(errors="replace")
