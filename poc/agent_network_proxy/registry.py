"""Caller registry: which network address belongs to which session.

Written by sandbox-controller when it creates or stops a sandbox, read by
agent-network-proxy to identify a caller from its source address. Stdlib
only, because it runs in both places.

Writers take an exclusive file lock around read-modify-write, so concurrent
writers cannot drop each other's updates, and replace the file atomically,
so a reader never sees a half-written registry. Readers re-read the file on
every lookup: it is small, and any cache keyed on a file timestamp can serve
stale data (coarse timestamps, e.g. on bind mounts), which here would mean
trusting a caller that was already unregistered.

Layout: {"callers": {ip: {...Caller, "sandbox": name}}, "epochs": {"tenant/session": newest_epoch}}
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path


class Superseded(Exception):
    """A sandbox tried to register but a newer executor has taken over its session (or its claim is gone)."""


@dataclass(frozen=True)
class Caller:
    session: str
    agent: str
    tenant: str
    epoch: int

    def __post_init__(self):
        if "/" in self.tenant:        # `key` joins tenant and session with "/": without this two pairs could share a key
            raise ValueError(f"tenant id {self.tenant!r} must not contain '/'")

    @property
    def key(self) -> str:
        """Sessions are scoped to their tenant: one tenant's session id must never touch another's."""
        return f"{self.tenant}/{self.session}"


class RegistryError(OSError):
    """The registry file cannot be read. An infrastructure failure, like any other unreadable state file."""


class Registry:
    def __init__(self, path: Path):
        self.path = Path(path)

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {"callers": {}, "epochs": {}}
        except json.JSONDecodeError as exc:
            raise RegistryError(f"{self.path} is not valid JSON: {exc}") from exc
        if not (isinstance(data, dict) and isinstance(data.get("callers"), dict) and isinstance(data.get("epochs"), dict)):
            raise RegistryError(f"{self.path} does not have the expected shape")
        return data

    @contextlib.contextmanager
    def transaction(self):
        """Hold the exclusive lock, yield the registry data to inspect and change, and write it back atomically
        when the block ends normally. An exception inside the block writes nothing. Everything that must be
        decided and recorded together (the controller's fence) happens inside one of these."""
        with open(self.path.with_name(self.path.name + ".lock"), "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            data = self._read()
            yield data
            fd, tmp = tempfile.mkstemp(dir=self.path.parent)
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(data, f)
                os.chmod(tmp, 0o644)
                os.replace(tmp, self.path)
            except BaseException:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(tmp)
                raise

    def _update(self, change) -> None:
        with self.transaction() as data:
            change(data)

    # -- controller side ---------------------------------------------------

    def register(self, ip: str, caller: Caller, sandbox: str, *, claimed: bool = False) -> None:
        """Record the sandbox's address. With `claimed`, the sandbox must still hold the claim the controller's
        fence made for it and its epoch must not have been superseded: a controller that lost a race must not
        end up with a trusted, running sandbox."""
        def change(data):
            if claimed and (f"pending:{sandbox}" not in data["callers"]
                            or caller.epoch < data["epochs"].get(caller.key, 0)):
                raise Superseded(f"{sandbox} no longer holds session {caller.key} (a newer executor took over)")
            data["callers"] = {k: c for k, c in data["callers"].items() if c["sandbox"] != sandbox}   # drops its pending claim
            data["callers"][ip] = {**asdict(caller), "sandbox": sandbox}
            data["epochs"][caller.key] = max(data["epochs"].get(caller.key, 0), caller.epoch)
        self._update(change)

    def unregister(self, sandbox: str) -> None:
        def change(data):
            data["callers"] = {ip: c for ip, c in data["callers"].items() if c["sandbox"] != sandbox}
        self._update(change)

    def bump_epoch(self, key: str, epoch: int) -> None:
        """Fence: from now on the proxy rejects any caller of session `key` (tenant/session) older than `epoch`."""
        def change(data):
            data["epochs"][key] = max(data["epochs"].get(key, 0), epoch)
        self._update(change)

    def newest_epoch(self, key: str) -> int:
        return self._read()["epochs"].get(key, 0)

    def sandboxes(self, key: str) -> list[tuple[str, int]]:
        """(sandbox name, epoch) for every sandbox registered to session `key` (tenant/session)."""
        return [(c["sandbox"], c["epoch"]) for c in self._read()["callers"].values()
                if f"{c['tenant']}/{c['session']}" == key]

    def clear(self) -> None:
        """Forget every caller (epochs are kept, so old executors stay fenced)."""
        self._update(lambda data: data.update(callers={}))

    # -- proxy side --------------------------------------------------------

    def lookup(self, ip: str) -> tuple[Caller, int] | None:
        """Return (caller, newest epoch known for its session), or None if unregistered."""
        data = self._read()
        entry = data["callers"].get(ip)
        if entry is None:
            return None
        caller = Caller(**{k: entry[k] for k in ("session", "agent", "tenant", "epoch")})
        return caller, data["epochs"].get(caller.key, caller.epoch)
