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

Layout: {"callers": {ip: {...Caller, "sandbox": name}}, "epochs": {session: newest_epoch}}
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class Caller:
    session: str
    agent: str
    tenant: str
    epoch: int


class Registry:
    def __init__(self, path: Path):
        self.path = Path(path)

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except FileNotFoundError:
            return {"callers": {}, "epochs": {}}

    def _update(self, change) -> None:
        with open(self.path.with_name(self.path.name + ".lock"), "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            data = self._read()
            change(data)
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

    # -- controller side ---------------------------------------------------

    def register(self, ip: str, caller: Caller, sandbox: str) -> None:
        def change(data):
            data["callers"][ip] = {**asdict(caller), "sandbox": sandbox}
            data["epochs"][caller.session] = max(data["epochs"].get(caller.session, 0), caller.epoch)
        self._update(change)

    def unregister(self, sandbox: str) -> None:
        def change(data):
            data["callers"] = {ip: c for ip, c in data["callers"].items() if c["sandbox"] != sandbox}
        self._update(change)

    def bump_epoch(self, session: str, epoch: int) -> None:
        """Fence: from now on the proxy rejects any caller of `session` older than `epoch`."""
        def change(data):
            data["epochs"][session] = max(data["epochs"].get(session, 0), epoch)
        self._update(change)

    def newest_epoch(self, session: str) -> int:
        return self._read()["epochs"].get(session, 0)

    def sandboxes(self, session: str) -> list[tuple[str, int]]:
        """(sandbox name, epoch) for every sandbox registered to `session`."""
        return [(c["sandbox"], c["epoch"]) for c in self._read()["callers"].values() if c["session"] == session]

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
        return caller, data["epochs"].get(caller.session, caller.epoch)
