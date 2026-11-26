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
import os
import subprocess
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from agent_network_proxy.registry import Caller, Registry, Superseded

LABEL = "agent-sandbox=1"
WORKDIR_MARKER = ".agent-sandbox-workdir"   # says "a sandbox controller created this directory, for this session"
PENDING_TTL_S = 60   # how long a claim on a session, made before its sandbox exists, protects it


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
    proxy_container: str | None = None   # default: found by its Compose labels, so naming scheme does not matter


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
    alive: bool = True       # False as soon as stop() is requested: no more commands
    stopped: bool = False    # True only once cleanup fully succeeded; until then stop() may be retried


class SandboxController:
    def __init__(self, image: str = "agent-sandbox:poc", proxy: ProxyConfig | None = None,
                 docker: str = "docker"):
        self.image, self.proxy, self.docker = image, proxy, docker
        self._by_session: dict[str, Sandbox] = {}     # keyed by Caller.key (tenant/session)
        self._proxy_name: str | None = None
        self.inspect_timeout_s = 10      # _exists() runs while the registry lock is held, so a hung daemon must not hang it

    # -- lifecycle ---------------------------------------------------------

    def create(self, workdir: Path, caller: Caller | None = None, limits: Limits = Limits(),
               user: str = "65534:65534") -> Sandbox:
        """`workdir` must be a directory dedicated to this sandbox: its mode is set to 0o777 so the
        unprivileged sandbox user can write to it (D7). It may already exist (a resumed session)."""
        workdir = Path(workdir).resolve()
        self._check_mount_path(workdir, "workdir")
        if self.proxy:
            self._check_mount_path(Path(self.proxy.ca_cert).resolve(), "ca_cert")
        if self.proxy and caller is None:
            raise SandboxError("a proxied sandbox needs a caller identity to register")
        owner = caller.key if caller else "-"
        self._check_workdir(workdir, owner)
        env = self._placeholder_env(caller) if self.proxy else {}   # all validation happens before anything is fenced or created
        name = f"sbx-{uuid.uuid4().hex[:12]}"
        if caller:
            self._fence(caller, name)
        sandbox = Sandbox(name=name, workdir=workdir, limits=limits, caller=caller)
        try:
            workdir.mkdir(parents=True, exist_ok=True)
            self._mark_workdir(workdir, owner)
            workdir.chmod(0o777)
            if self.proxy:
                sandbox.network = f"agentpoc_{name}"
                self._docker(["network", "create", "--internal", "--label", LABEL, sandbox.network], "create network")
                self._docker(["network", "connect", "--alias", "proxy", sandbox.network, self._proxy_container()],
                             "attach proxy to sandbox network")
            self._docker(self._run_args(sandbox, user, env), "create sandbox")
            if self.proxy:
                sandbox.ip = self._docker(
                    ["inspect", "-f", f'{{{{(index .NetworkSettings.Networks "{sandbox.network}").IPAddress}}}}', name],
                    "inspect sandbox")
                if not sandbox.ip:
                    raise SandboxError(f"{name} has no address on its network (did it exit immediately?)")
                try:
                    self.proxy.registry.register(sandbox.ip, caller, name, claimed=True)
                except Superseded as exc:
                    raise SandboxError(str(exc)) from exc
        except Exception as exc:
            try:
                self.stop(sandbox)
            except Exception as cleanup:   # noqa: BLE001 - say what was left behind instead of hiding it
                raise SandboxError(f"{exc}; ALSO cleaning up {name} (network {sandbox.network}) failed: {cleanup}. "
                                   "Run reap() to remove it.") from exc
            raise
        if caller:
            self._by_session[caller.key] = sandbox
        return sandbox

    @staticmethod
    def _check_workdir(workdir: Path, owner: str) -> None:
        """A directory that already holds files is only reused if a controller marked it for this same session.
        The marker is inside the sandbox's own /work, so it is untrusted: it must be a regular file, not a symlink
        (which would make the host follow it), and it names the owner so one session cannot mount another's files."""
        if not (workdir.exists() and any(workdir.iterdir())):
            return
        marker = workdir / WORKDIR_MARKER
        if marker.is_symlink() or not marker.is_file():
            raise SandboxError(f"workdir {workdir} is not dedicated to a sandbox: it exists, is not empty, and was not "
                               "created by a sandbox controller (its mode would be changed to 0777 and it would be mounted writable)")
        if marker.read_text() != owner:
            raise SandboxError(f"workdir {workdir} belongs to {marker.read_text()!r}, not {owner!r}")

    @staticmethod
    def _mark_workdir(workdir: Path, owner: str) -> None:
        marker = workdir / WORKDIR_MARKER
        if not marker.is_symlink() and marker.exists():
            return                                   # a resumed session: already checked by _check_workdir
        fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
        with os.fdopen(fd, "w") as f:
            f.write(owner)

    def _run_args(self, sandbox: Sandbox, user: str, env: dict[str, str]) -> list[str]:
        limits = sandbox.limits
        return [
            "run", "-d", "--rm", "--pull", "never", "--name", sandbox.name, "--label", LABEL,
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
        """Kill the sandbox, forget it, and remove its network. Safe to call twice. Each step runs even if an
        earlier one failed (a broken registry must not stop the kill); the first failure is raised at the end.
        The network is only removed once the container is confirmed gone."""
        sandbox.alive = False
        errors: list[Exception] = []

        def attempt(step, *args, **kwargs) -> bool:
            try:
                step(*args, **kwargs)
                return True
            except Exception as exc:  # noqa: BLE001 - collected and re-raised below
                errors.append(exc)
                return False

        if self.proxy:
            attempt(self.proxy.registry.unregister, sandbox.name)   # first: no window where a dying sandbox is trusted
        attempt(subprocess.run, [self.docker, "kill", sandbox.name], capture_output=True)
        gone = attempt(self._wait_until_gone, sandbox.name)
        if sandbox.network and gone:
            attempt(self._remove_network, sandbox.network)
        if not errors and sandbox.caller and self._by_session.get(sandbox.caller.key) is sandbox:
            del self._by_session[sandbox.caller.key]   # kept after a failed stop: it may still be running, and a fence must see it
        sandbox.stopped = not errors
        if errors:
            raise errors[0]

    def reap(self) -> None:
        """Remove every sandbox and sandbox network this label owns, and forget every registered caller:
        recovery after a crash. Keeps going past a failure and raises the first one at the end."""
        errors: list[Exception] = []
        ids = self._docker(["ps", "-aq", "--filter", f"label={LABEL}"], "list sandboxes").split()
        if self.proxy:
            self.proxy.registry.clear()             # first: stop trusting every address, including those of sandboxes already gone
        if ids:
            subprocess.run([self.docker, "kill", *ids], capture_output=True)
        for container in ids:
            try:
                self._wait_until_gone(container)
            except SandboxError as exc:
                errors.append(exc)
        for net in self._docker(["network", "ls", "-q", "--filter", f"label={LABEL}", "--format", "{{.Name}}"],
                                "list networks").split():
            try:
                self._remove_network(net)
            except SandboxError as exc:
                errors.append(exc)
        self._by_session.clear()
        if errors:
            raise errors[0]

    def _wait_until_gone(self, name: str, timeout_s: float = 10) -> None:
        """`docker kill` returns once the signal is sent; `--rm` removes the container a moment later.
        Return only when it is really gone, so "stopped" means stopped (and its network can be removed)."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if not self._exists(name):
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
        done = subprocess.run([self.docker, "network", "rm", network], capture_output=True, text=True)
        if done.returncode != 0 and "No such network" not in done.stderr and "not found" not in done.stderr:
            raise SandboxError(f"could not remove network {network}: {done.stderr.strip()}")

    def _proxy_container(self) -> str:
        if self.proxy.proxy_container:
            return self.proxy.proxy_container
        if self._proxy_name is None:
            found = self._docker(["ps", "--filter", "label=com.docker.compose.service=proxy", "--filter",
                                  "label=com.docker.compose.project=agentpoc", "--format", "{{.Names}}"],
                                 "find the proxy container").split()
            if len(found) != 1:
                raise SandboxError(f"expected exactly one running proxy container (run `make demo` or `python3 stack.py up`), found {found}")
            self._proxy_name = found[0]
        return self._proxy_name

    @staticmethod
    def _check_mount_path(path: Path, what: str) -> None:
        if "," in str(path) or "\n" in str(path):
            raise SandboxError(f"{what} path must not contain ',' or a newline: it is spliced into a --mount option")

    def _placeholder_env(self, caller: Caller) -> dict[str, str]:
        """The placeholder tokens this agent's sandbox gets, as {env var: placeholder}. Refuses an agent that
        does not belong to the caller's tenant, and one env var standing for two different placeholders
        (one would silently win, and calls to the other destination would go out with no credential)."""
        try:
            agent = json.loads(self.proxy.grants_path.read_text())["agents"].get(caller.agent)
            if agent is None or agent["tenant"] != caller.tenant:
                raise SandboxError(f"agent/tenant mismatch: {caller.agent} is not a member of tenant {caller.tenant}")
            env: dict[str, str] = {}
            for dest in agent["destinations"].values():
                if env.setdefault(dest["env_var"], dest["placeholder"]) != dest["placeholder"]:
                    raise SandboxError(f"env var {dest['env_var']} is mapped to two different placeholders")
            return env
        except (ValueError, KeyError, TypeError, AttributeError) as exc:    # unparsable or wrongly shaped grants
            raise SandboxError(f"cannot read the grants for {caller.agent}: {type(exc).__name__}: {exc}") from exc

    def _fence(self, caller: Caller, name: str) -> None:
        """A newer epoch supersedes older ones: their sandboxes are stopped. An older epoch is refused, and so is
        an equal one while another sandbox for the session is alive: two sandboxes must never hold one session.
        With a proxy, the check, the epoch bump and a claim on the session for `name` happen inside one registry
        transaction, so two controllers racing for the same session cannot both win. Fencing happens before the
        new sandbox exists: if creating it then fails, the old one stays stopped and the epoch stays raised,
        because the newer executor now owns the session and retries."""
        old = self._by_session.get(caller.key)
        if old and caller.epoch <= old.caller.epoch:
            if caller.epoch == old.caller.epoch and not self._exists(old.name):
                self.stop(old)                       # it expired on its own: the same epoch may start again
                old = None
            else:
                raise SandboxError(f"stale epoch {caller.epoch} for session {caller.session} (current {old.caller.epoch})")
        superseded: list[str] = []
        if self.proxy:
            with self.proxy.registry.transaction() as data:
                if caller.epoch < (newest := data["epochs"].get(caller.key, 0)):
                    raise SandboxError(f"stale epoch {caller.epoch} for session {caller.session} (registry has {newest})")
                for ip, entry in list(data["callers"].items()):
                    if f"{entry['tenant']}/{entry['session']}" != caller.key or (old is not None and entry["sandbox"] == old.name):
                        continue
                    claimed = "pending" in entry and time.time() - entry["pending"] < PENDING_TTL_S
                    if claimed or self._exists(entry["sandbox"]):
                        if entry["epoch"] >= caller.epoch:
                            raise SandboxError(f"session {caller.session} already has a live sandbox "
                                               f"{entry['sandbox']} at epoch {entry['epoch']}")
                        superseded.append(entry["sandbox"])
                    else:
                        del data["callers"][ip]      # a stale entry for a sandbox that is gone
                data["epochs"][caller.key] = max(caller.epoch, newest)
                data["callers"][f"pending:{name}"] = {**asdict(caller), "sandbox": name, "pending": time.time()}
        try:
            for victim in superseded:                # stopping sandboxes is slow, so it happens outside the lock
                self.stop(Sandbox(name=victim, workdir=Path("."), limits=Limits(), network=f"agentpoc_{victim}"))
            if old:
                self.stop(old)
        except Exception:
            if self.proxy:
                with contextlib.suppress(Exception):
                    self.proxy.registry.unregister(name)     # do not leave a claim that blocks the retry
            raise

    def _exists(self, name: str) -> bool:
        """True/False only when the daemon actually answered. Any other failure (daemon unreachable, ...) is an
        error: "I cannot tell" must never be read as "it is gone"."""
        try:
            done = subprocess.run([self.docker, "inspect", name], capture_output=True, text=True,
                                  timeout=self.inspect_timeout_s)
        except subprocess.TimeoutExpired as exc:
            raise SandboxError(f"cannot tell whether {name} exists: docker inspect timed out after {self.inspect_timeout_s}s") from exc
        if done.returncode == 0:
            return True
        if "no such" in done.stderr.lower():
            return False
        raise SandboxError(f"cannot tell whether {name} exists: {done.stderr.strip() or 'docker inspect failed'}")

    def _network_args(self, sandbox: Sandbox, env: dict[str, str]) -> list[str]:
        if self.proxy is None:
            return ["--network", "none"]
        env = dict(env)
        ca = "/etc/ssl/proxy-ca.pem"
        env.update({k: ca for k in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
                                    "NODE_EXTRA_CA_CERTS", "GIT_SSL_CAINFO")})
        env.update({k: self.proxy.proxy_url for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")})
        args = ["--network", sandbox.network,
                "--mount", f"type=bind,src={Path(self.proxy.ca_cert).resolve()},dst={ca},readonly"]
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
            except Exception:  # noqa: BLE001 - the caller must still learn the command was cut off; the executor retries
                pass           # the cleanup because stop() leaves `sandbox.stopped` False when it fails
            finally:
                proc.wait()
        out.join(), err.join()
        if not timed_out and proc.returncode != 0 and self._vanished(sandbox, err.text()):
            with contextlib.suppress(Exception):   # the container is already gone; leftover cleanup is retried later
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
        if running.returncode == 0:
            return running.stdout.strip() != "true"
        if "no such" in running.stderr.lower():
            return True
        raise SandboxError(f"cannot tell whether {sandbox.name} is running: {running.stderr.strip()}")

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
