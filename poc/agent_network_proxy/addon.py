"""agent-network-proxy: the only route out of a sandbox, and the only holder of real credentials.

A mitmproxy addon. For every request it:
  1. identifies the caller from the connection's source address (never from anything the request claims),
  2. rejects callers whose epoch is stale,
  3. checks the destination against that agent's grants,
  4. applies the tenant's rate limit,
  5. swaps the placeholder token for the real credential from the secrets file,
  6. scrubs the real credential out of the response,
  7. writes an audit line: the credential's NAME, never its value.
Fails closed: any doubt is a denial.
"""
from __future__ import annotations

import json
import os
import time
from collections import defaultdict, deque
from pathlib import Path

from mitmproxy import http

from agent_network_proxy.registry import Registry

CONFIG = Path(os.environ.get("CONFIG_DIR", "/config"))
REGISTRY = Path(os.environ.get("REGISTRY", "/registry/registry.json"))
AUDIT = Path(os.environ.get("AUDIT", "/audit/audit.jsonl"))


class RateLimiter:
    """Sliding window per tenant. In-memory, so it is per proxy replica (see README gaps)."""

    def __init__(self):
        self._hits: dict[str, deque] = defaultdict(deque)

    def allow(self, tenant: str, requests: int, per_seconds: float, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        hits = self._hits[tenant]
        while hits and now - hits[0] >= per_seconds:
            hits.popleft()
        if len(hits) >= requests:
            return False
        hits.append(now)
        return True


class Gatekeeper:
    def __init__(self):
        self.registry = Registry(REGISTRY)
        self.limiter = RateLimiter()

    # -- policy ------------------------------------------------------------

    def _check(self, flow: http.HTTPFlow, *, count_rate: bool):
        """Return (caller, grant, None) if allowed, else (caller|None, None, reason)."""
        found = self.registry.lookup(flow.client_conn.peername[0])
        if found is None:
            return None, None, "unknown caller"
        caller, newest_epoch = found
        if caller.epoch < newest_epoch:
            return caller, None, "stale epoch"
        grants = json.loads((CONFIG / "grants.json").read_text())
        grant = grants["agents"].get(caller.agent, {}).get("destinations", {}).get(flow.request.host)
        if grant is None:
            return caller, None, "destination not granted"
        if count_rate:
            rl = grants["tenants"][caller.tenant]["rate_limit"]
            if not self.limiter.allow(caller.tenant, rl["requests"], rl["per_seconds"]):
                return caller, None, "rate limit"
        return caller, grant, None

    # -- mitmproxy hooks ---------------------------------------------------

    def http_connect(self, flow: http.HTTPFlow) -> None:
        caller, _, reason = self._check(flow, count_rate=False)
        if reason:
            self._deny(flow, caller, reason)

    def request(self, flow: http.HTTPFlow) -> None:
        caller, grant, reason = self._check(flow, count_rate=True)
        if reason:
            return self._deny(flow, caller, reason)
        secret = json.loads((CONFIG / "secrets.json").read_text())[caller.tenant][grant["credential"]]
        placeholder = grant["placeholder"]
        for name, value in flow.request.headers.items():
            if placeholder in value:
                flow.request.headers[name] = value.replace(placeholder, secret)
        for name, value in flow.request.query.items():
            if placeholder in value:
                flow.request.query[name] = value.replace(placeholder, secret)
        flow.metadata.update(caller=caller, credential=grant["credential"], secret=secret)

    def response(self, flow: http.HTTPFlow) -> None:
        secret = flow.metadata.get("secret")
        if secret is None:
            return
        for name, value in flow.response.headers.items():
            flow.response.headers[name] = value.replace(secret, "[REDACTED]")
        body = flow.response.get_content(strict=False) or b""
        flow.response.set_content(body.replace(secret.encode(), b"[REDACTED]"))
        self._audit(flow, flow.metadata["caller"], "allow", None, flow.metadata["credential"],
                    flow.response.status_code)

    # -- helpers -----------------------------------------------------------

    def _deny(self, flow, caller, reason: str) -> None:
        code = 429 if reason == "rate limit" else 403
        flow.response = http.Response.make(code, f"denied: {reason}\n", {"Content-Type": "text/plain"})
        self._audit(flow, caller, "deny", reason, None, code)

    def _audit(self, flow, caller, decision, reason, credential, status) -> None:
        line = {
            "ts": time.time(), "decision": decision, "reason": reason, "status": status,
            "tenant": caller and caller.tenant, "agent": caller and caller.agent,
            "session": caller and caller.session, "epoch": caller and caller.epoch,
            "method": flow.request.method, "host": flow.request.host,
            "path": flow.request.path.split("?")[0],   # no query string: it may carry a token
            "credential": credential,                  # a name such as "tenant-a/github"; never a value
        }
        with AUDIT.open("a") as f:
            f.write(json.dumps(line) + "\n")


addons = [Gatekeeper()]
