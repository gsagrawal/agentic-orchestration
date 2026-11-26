"""Caller registry: which network address belongs to which session.

Written by sandbox-controller when it creates or stops a sandbox, read by
agent-network-proxy to identify a caller from its source address. Stdlib
only, because it runs in both places. The file is replaced atomically, so a
reader never sees a half-written registry.

Layout: {"callers": {ip: {...Caller, "sandbox": name}}, "epochs": {session: newest_epoch}}
"""
from __future__ import annotations

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
        self._cache: tuple[float, dict] | None = None

    def _read(self) -> dict:
        try:
            mtime = self.path.stat().st_mtime_ns
        except FileNotFoundError:
            return {"callers": {}, "epochs": {}}
        if self._cache is None or self._cache[0] != mtime:
            self._cache = (mtime, json.loads(self.path.read_text()))
        return self._cache[1]

    def _write(self, data: dict) -> None:
        fd, tmp = tempfile.mkstemp(dir=self.path.parent)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.chmod(tmp, 0o644)
        os.replace(tmp, self.path)

    # -- controller side ---------------------------------------------------

    def register(self, ip: str, caller: Caller, sandbox: str) -> None:
        data = json.loads(json.dumps(self._read()))
        data["callers"][ip] = {**asdict(caller), "sandbox": sandbox}
        data["epochs"][caller.session] = max(data["epochs"].get(caller.session, 0), caller.epoch)
        self._write(data)

    def unregister(self, sandbox: str) -> None:
        data = json.loads(json.dumps(self._read()))
        data["callers"] = {ip: c for ip, c in data["callers"].items() if c["sandbox"] != sandbox}
        self._write(data)

    def bump_epoch(self, session: str, epoch: int) -> None:
        """Fence: from now on the proxy rejects any caller of `session` older than `epoch`."""
        data = json.loads(json.dumps(self._read()))
        data["epochs"][session] = max(data["epochs"].get(session, 0), epoch)
        self._write(data)

    # -- proxy side --------------------------------------------------------

    def lookup(self, ip: str) -> tuple[Caller, int] | None:
        """Return (caller, newest epoch known for its session), or None if unregistered."""
        data = self._read()
        entry = data["callers"].get(ip)
        if entry is None:
            return None
        caller = Caller(**{k: entry[k] for k in ("session", "agent", "tenant", "epoch")})
        return caller, data["epochs"].get(caller.session, caller.epoch)
