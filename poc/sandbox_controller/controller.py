"""sandbox-controller: creates, limits, and stops sandboxes.

The only component that talks to Docker. It never sees a real secret: it
installs a placeholder token and the proxy's *public* certificate, and
registers the sandbox with the proxy. Shells out to the docker CLI so there
is no third-party dependency. Every docker flag is explained in
docs/docker-flags.md.
"""
from __future__ import annotations

import contextlib
import json
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from agent_network_proxy.registry import Caller, Registry

LABEL = "agent-sandbox=1"


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
    proxy_container: str = "agentpoc-proxy-1"


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


class SandboxGone(SandboxError):
    """The sandbox no longer exists (deadline hit, or killed), so the command was never started."""


@dataclass
class Sandbox:
    name: str
    workdir: Path
    limits: Limits
    caller: Caller | None = None
    ip: str | None = None           # its address on its own private network (proxied sandboxes only)
    network: str | None = None
    alive: bool = True


class SandboxController:
    def __init__(self, image: str = "agent-sandbox:poc", proxy: ProxyConfig | None = None,
                 docker: str = "docker"):
        self.image, self.proxy, self.docker = image, proxy, docker
        self._by_session: dict[str, Sandbox] = {}

    # -- lifecycle ---------------------------------------------------------

    def create(self, workdir: Path, caller: Caller | None = None, limits: Limits = Limits(),
               user: str = "65534:65534") -> Sandbox:
        """`workdir` must be a directory dedicated to this sandbox: its mode is set to 0o777 so the
        unprivileged sandbox user can write to it (D7). It may already exist (a resumed session)."""
        workdir = Path(workdir).resolve()
        if "," in str(workdir) or "\n" in str(workdir):
            raise SandboxError("workdir path must not contain ',' or a newline: it is spliced into a --mount option")
        if self.proxy and caller is None:
            raise SandboxError("a proxied sandbox needs a caller identity to register")
        env = self._placeholder_env(caller) if self.proxy else {}   # validates before anything is fenced or created
        if caller:
            self._fence(caller)
        workdir.mkdir(parents=True, exist_ok=True)
        workdir.chmod(0o777)
        name = f"sbx-{uuid.uuid4().hex[:12]}"
        sandbox = Sandbox(name=name, workdir=workdir, limits=limits, caller=caller)
        try:
            if self.proxy:
                sandbox.network = f"agentpoc_{name}"
                self._docker(["network", "create", "--internal", "--label", LABEL, sandbox.network], "create network")
                self._docker(["network", "connect", "--alias", "proxy", sandbox.network, self.proxy.proxy_container],
                             "attach proxy to sandbox network")
            self._docker(self._run_args(sandbox, user, env), "create sandbox")
            if self.proxy:
                sandbox.ip = self._docker(
                    ["inspect", "-f", f'{{{{(index .NetworkSettings.Networks "{sandbox.network}").IPAddress}}}}', name],
                    "inspect sandbox")
                self.proxy.registry.register(sandbox.ip, caller, name)
        except Exception:
            with contextlib.suppress(Exception):    # cleanup must not replace the error that got us here
                self.stop(sandbox)
            raise
        if caller:
            self._by_session[caller.session] = sandbox
        return sandbox

    def _run_args(self, sandbox: Sandbox, user: str, env: dict[str, str]) -> list[str]:
        limits = sandbox.limits
        return [
            "run", "-d", "--rm", "--name", sandbox.name, "--label", LABEL,
            "--read-only",
            "--tmpfs", f"/tmp:rw,noexec,nosuid,nodev,size={limits.tmpfs_mb}m",
            "--mount", f"type=bind,src={sandbox.workdir},dst=/work",
            "--workdir", "/work",
            "--user", user,
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--cpus", str(limits.cpus),
            "--memory", f"{limits.memory_mb}m", "--memory-swap", f"{limits.memory_mb}m",
            "--pids-limit", str(limits.pids),
            "--init",
            "--env", "HOME=/tmp", "--env", "PYTHONDONTWRITEBYTECODE=1",
            *self._network_args(sandbox, env),
            self.image, "sleep", str(limits.deadline_s),
        ]

    def stop(self, sandbox: Sandbox) -> None:
        """Kill the sandbox, forget it, and remove its network. Safe to call twice. Every cleanup step runs
        even if an earlier one fails; the first failure is then raised."""
        sandbox.alive = False
        try:
            if self.proxy:
                self.proxy.registry.unregister(sandbox.name)  # first: no window where a dying sandbox is still trusted
            subprocess.run([self.docker, "kill", sandbox.name], capture_output=True)
            self._wait_until_gone(sandbox.name)
        finally:
            if sandbox.network:
                self._remove_network(sandbox.network)
            if sandbox.caller and self._by_session.get(sandbox.caller.session) is sandbox:
                del self._by_session[sandbox.caller.session]

    def reap(self) -> None:
        """Remove every sandbox and sandbox network this controller's label owns, e.g. after a crash."""
        ids = self._docker(["ps", "-aq", "--filter", f"label={LABEL}"], "list sandboxes").split()
        if ids:
            subprocess.run([self.docker, "kill", *ids], capture_output=True)
            for container in ids:
                self._wait_until_gone(container)
        for net in self._docker(["network", "ls", "-q", "--filter", f"label={LABEL}", "--format", "{{.Name}}"],
                                "list networks").split():
            self._remove_network(net)
        self._by_session.clear()

    def _wait_until_gone(self, name: str, timeout_s: float = 10) -> None:
        """`docker kill` returns once the signal is sent; `--rm` removes the container a moment later.
        Return only when it is really gone, so "stopped" means stopped (and its network can be removed)."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if subprocess.run([self.docker, "inspect", name], capture_output=True).returncode != 0:
                return
            time.sleep(0.05)
        raise SandboxError(f"{name} still exists {timeout_s}s after kill")

    def _remove_network(self, network: str) -> None:
        """Detach whatever is still attached (the proxy), then remove the network. Works from the network's own
        state, so it does not matter which controller instance, or whether one with a proxy, calls it."""
        attached = subprocess.run([self.docker, "network", "inspect", network, "--format",
                                   "{{range .Containers}}{{.Name}} {{end}}"], capture_output=True, text=True).stdout.split()
        for container in attached:
            subprocess.run([self.docker, "network", "disconnect", "-f", network, container], capture_output=True)
        subprocess.run([self.docker, "network", "rm", network], capture_output=True)

    def _placeholder_env(self, caller: Caller) -> dict[str, str]:
        """The placeholder tokens this agent's sandbox gets, as {env var: placeholder}. Refuses an agent that
        does not belong to the caller's tenant, and one env var standing for two different placeholders
        (one would silently win, and calls to the other destination would go out with no credential)."""
        agent = json.loads(self.proxy.grants_path.read_text())["agents"].get(caller.agent)
        if agent is None or agent["tenant"] != caller.tenant:
            raise SandboxError(f"agent/tenant mismatch: {caller.agent} is not a member of tenant {caller.tenant}")
        env: dict[str, str] = {}
        for dest in agent["destinations"].values():
            if env.setdefault(dest["env_var"], dest["placeholder"]) != dest["placeholder"]:
                raise SandboxError(f"env var {dest['env_var']} is mapped to two different placeholders")
        return env

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

    def _network_args(self, sandbox: Sandbox, env: dict[str, str]) -> list[str]:
        if self.proxy is None:
            return ["--network", "none"]
        env = dict(env)
        ca = "/etc/ssl/proxy-ca.pem"
        env.update({k: ca for k in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
                                    "NODE_EXTRA_CA_CERTS", "GIT_SSL_CAINFO")})
        env.update({k: self.proxy.proxy_url for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")})
        args = ["--network", sandbox.network,
                "--mount", f"type=bind,src={self.proxy.ca_cert},dst={ca},readonly"]
        for k, v in env.items():
            args += ["--env", f"{k}={v}"]
        return args

    # -- execution ---------------------------------------------------------

    def exec(self, sandbox: Sandbox, argv: list[str], timeout_s: float = 30) -> ExecResult:
        """Run argv in the sandbox. On timeout the whole sandbox is killed:
        killing only the `docker exec` client would leave the process running."""
        if not sandbox.alive:
            raise SandboxError(f"{sandbox.name} is no longer running")
        cap = sandbox.limits.max_output_bytes
        start = time.monotonic()
        proc = subprocess.Popen([self.docker, "exec", sandbox.name, *argv],
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = _Capture(proc.stdout, cap), _Capture(proc.stderr, cap)
        timed_out = False
        try:
            proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            proc.kill()
            try:
                self.stop(sandbox)
            finally:
                proc.wait()
        out.join(), err.join()
        if not timed_out and proc.returncode != 0 and self._vanished(sandbox, err.text()):
            self.stop(sandbox)
            raise SandboxGone(f"{sandbox.name} is gone (deadline reached or killed); the command was not started")
        return ExecResult(exit_code=proc.returncode, stdout=out.text(), stderr=err.text(),
                          timed_out=timed_out, duration_s=time.monotonic() - start,
                          truncated=out.truncated or err.truncated)

    def _vanished(self, sandbox: Sandbox, stderr: str) -> bool:
        """`docker exec` could not start the command because the container is not running. The stderr text
        is only a hint (a command can print anything), so confirm with the daemon."""
        if "Error response from daemon" not in stderr:
            return False
        running = subprocess.run([self.docker, "inspect", "-f", "{{.State.Running}}", sandbox.name],
                                 capture_output=True, text=True)
        return running.returncode != 0 or running.stdout.strip() != "true"

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
