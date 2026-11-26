"""agent-network-proxy: the only route out of a sandbox, and the only holder of real credentials.

A mitmproxy addon. For every request it:
  1. identifies the caller from the connection's source address (never from anything the request claims),
  2. rejects callers whose epoch is stale, or whose agent does not belong to their tenant,
  3. checks the destination (host, port, and HTTPS) against that agent's grants,
  4. applies the tenant's rate limit,
  5. writes the audit line BEFORE the credential is used: no audit record, no credential,
  6. swaps the placeholder token for the real credential from the secrets file,
  7. scrubs the real credential out of the response and audits the outcome.
Fails closed: any doubt, including an unexpected exception in any hook, is a denial. Only HTTP request/response
traffic is understood, so everything else is refused: WebSocket upgrades are denied here and the raw-TCP
fallback is switched off in docker-compose.yml (`rawtcp=false`); neither could be audited or scrubbed.
"""
from __future__ import annotations

import base64
import functools
import json
import os
import time
from collections import defaultdict, deque
from urllib.parse import quote
from pathlib import Path

from mitmproxy import ctx, http

from agent_network_proxy.registry import Registry

CONFIG = Path(os.environ.get("CONFIG_DIR", "/config"))
REGISTRY = Path(os.environ.get("REGISTRY", "/registry/registry.json"))
AUDIT = Path(os.environ.get("AUDIT", "/audit/audit.jsonl"))
REDACTED = b"[REDACTED]"


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


def swap(value: bytes, placeholder: bytes, real: bytes) -> bytes:
    """Replace the placeholder in a header value, including inside HTTP Basic credentials (how git sends a token)."""
    if value[:6].lower() == b"basic ":
        try:
            decoded = base64.b64decode(value[6:], validate=True)
        except ValueError:
            decoded = b""
        if placeholder in decoded:
            return value[:6] + base64.b64encode(decoded.replace(placeholder, real))
    return value.replace(placeholder, real)


def fail_closed(hook):
    """Any exception in a hook becomes a denial instead of a request that sails through."""
    @functools.wraps(hook)
    def wrapper(self, flow):
        try:
            hook(self, flow)
        except Exception as exc:  # noqa: BLE001 - deliberately broad: the alternative is fail-open
            ctx.log.error(f"{hook.__name__}: {type(exc).__name__}: {exc}")
            self._deny(flow, self._caller(flow), "internal error")
    return wrapper


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
        agent = grants["agents"].get(caller.agent)
        if agent is None:
            return caller, None, "unknown agent"
        if agent["tenant"] != caller.tenant:
            return caller, None, "agent/tenant mismatch"
        grant = agent["destinations"].get(flow.request.host)
        if grant is None:
            return caller, None, "destination not granted"
        if flow.request.port not in grant.get("ports", [443]):
            return caller, None, "port not granted"
        if flow.request.method != "CONNECT" and flow.request.scheme != "https":
            return caller, None, "plaintext not permitted"
        if flow.request.headers.get("Upgrade") or "upgrade" in flow.request.headers.get("Connection", "").lower():
            return caller, None, "upgrade not permitted"      # WebSocket frames would bypass audit and scrubbing
        placeholder = grant["placeholder"]
        in_query_name = any(placeholder in key for key, _ in flow.request.query.items(multi=True))
        if in_query_name or placeholder in flow.request.path.split("?")[0] or placeholder.encode() in (flow.request.raw_content or b""):
            return caller, None, "placeholder in an unsupported position"      # fail loudly: it would go out as a fake token
        if count_rate:
            rl = grants["tenants"][caller.tenant]["rate_limit"]
            if not self.limiter.allow(caller.tenant, rl["requests"], rl["per_seconds"]):
                return caller, None, "rate limit"
        return caller, grant, None

    # -- mitmproxy hooks ---------------------------------------------------

    @fail_closed
    def http_connect(self, flow: http.HTTPFlow) -> None:
        caller, _, reason = self._check(flow, count_rate=False)
        if reason:
            self._deny(flow, caller, reason)

    @fail_closed
    def request(self, flow: http.HTTPFlow) -> None:
        caller, grant, reason = self._check(flow, count_rate=False)
        if reason:
            return self._deny(flow, caller, reason)
        secret = json.loads((CONFIG / "secrets.json").read_text())[caller.tenant][grant["credential"]]
        if not secret or not grant["placeholder"]:
            raise ValueError("empty secret or placeholder")       # an empty needle would corrupt every response
        rl = json.loads((CONFIG / "grants.json").read_text())["tenants"][caller.tenant]["rate_limit"]
        if not self.limiter.allow(caller.tenant, rl["requests"], rl["per_seconds"]):       # only now: errors above cost nothing
            return self._deny(flow, caller, "rate limit")
        placeholder, real = grant["placeholder"].encode(), secret.encode()
        # Compute every rewrite first, so nothing can fail between the audit line and the request being released.
        swapped = [(k, swap(v, placeholder, real)) for k, v in flow.request.headers.fields]
        echoes = [new for (_, old), (_, new) in zip(flow.request.headers.fields, swapped) if new != old]   # e.g. a Basic blob
        path, _, raw_query = flow.request.path.partition("?")      # the raw text: every other byte is left as the client sent it
        # A secret must not change how the query parses upstream (`&`, `#`, `+`, `=`, spaces), so it is percent-encoded.
        query = raw_query.replace(grant["placeholder"], quote(secret, safe="")) if grant["placeholder"] in raw_query else None
        self._audit(flow, caller, "allow", None, grant["credential"], None)   # before the credential is used
        flow.request.headers.fields = tuple(swapped)
        if query is not None:
            flow.request.path = f"{path}?{query}"
        flow.metadata.update(caller=caller, credential=grant["credential"], secret=secret, echoes=echoes)

    @fail_closed
    def response(self, flow: http.HTTPFlow) -> None:
        secret = flow.metadata.get("secret")
        if secret is None:
            return
        # What to remove: the secret itself, and every header value we rewrote (a Basic blob contains the secret
        # only in base64, which a plain search would miss when the destination echoes the header back).
        echoes = flow.metadata.get("echoes", [])
        needles = [n for n in (secret.encode(), base64.b64encode(secret.encode()),
                               *echoes, *(e.split(b" ", 1)[-1] for e in echoes)) if n]       # never an empty needle
        def scrub(data: bytes) -> bytes:
            for needle in needles:
                data = data.replace(needle, REDACTED)
            return data
        flow.response.headers.fields = tuple((k, scrub(v)) for k, v in flow.response.headers.fields)
        try:
            body = flow.response.get_content(strict=True)     # undecodable means unscrubbable: refuse, do not guess
        except ValueError as exc:
            raise ValueError(f"response body cannot be decoded, so it cannot be scrubbed: {exc}") from exc
        if body and any(n in body for n in needles):
            flow.response.set_content(scrub(body))
        self._audit(flow, flow.metadata["caller"], "result", None, flow.metadata["credential"],
                    flow.response.status_code)

    def error(self, flow: http.HTTPFlow) -> None:
        """A request failed after it was allowed (upstream reset, client left). Still leaves a record."""
        self._audit_failure(flow)

    def http_connect_error(self, flow: http.HTTPFlow) -> None:
        """The tunnel to an allowed destination could not be opened (DNS failure, connection refused)."""
        self._audit_failure(flow)

    # -- helpers -----------------------------------------------------------

    def _caller(self, flow):
        """Best-effort identity for a log line. Never raises: it runs inside failure handlers, and a handler
        that fails would let the request through."""
        try:
            if caller := flow.metadata.get("caller"):
                return caller
            found = self.registry.lookup(flow.client_conn.peername[0])
            return found[0] if found else None
        except Exception:  # noqa: BLE001
            return None

    def _audit_failure(self, flow) -> None:
        if flow.metadata.get("denied"):
            return
        try:
            reason = flow.error.msg if flow.error else (flow.response.text.strip() if flow.response else "failed")
            self._audit(flow, self._caller(flow), "error", reason, flow.metadata.get("credential"), None)
        except Exception as exc:  # noqa: BLE001 - nothing left to deny; make the loss visible
            ctx.log.error(f"audit failed while recording an error: {type(exc).__name__}: {exc}")

    def _deny(self, flow, caller, reason: str) -> None:
        code = {"rate limit": 429, "internal error": 500}.get(reason, 403)
        flow.metadata["denied"] = True      # already audited as a denial; the failure hooks must not add a second line
        flow.response = http.Response.make(code, f"denied: {reason}\n", {"Content-Type": "text/plain"})
        try:
            self._audit(flow, caller, "deny", reason, None, code)
        except Exception as exc:  # noqa: BLE001 - the denial already stands; do not let logging undo it
            ctx.log.error(f"audit failed while denying: {type(exc).__name__}: {exc}")

    def _audit(self, flow, caller, decision, reason, credential, status) -> None:
        line = {
            "ts": time.time(), "decision": decision, "reason": reason, "status": status,
            "tenant": caller and caller.tenant, "agent": caller and caller.agent,
            "session": caller and caller.session, "epoch": caller and caller.epoch,
            "method": flow.request.method, "host": flow.request.host,
            "path": flow.request.path.split("?")[0],   # no query string: it may carry a token
            "credential": credential,                  # a name such as "github"; never a value
        }
        with AUDIT.open("a") as f:
            f.write(json.dumps(line) + "\n")


addons = [Gatekeeper()]
