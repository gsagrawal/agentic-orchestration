# POC vs. Production — Scope Exceptions Tracker

Status: living document. Every time the POC deliberately takes a shortcut
against a locked requirement (for speed, cost, or simplicity), it gets
recorded here with a priority for closing the gap before real tenant data
or production traffic depends on it. Nothing in here is accidental scope
creep — each row is a conscious, called-out trade-off, not a quiet
reversal of the requirement it defers.

Referenced from: [requirements.md](requirements.md).

## Priority levels

- **P0** — must be closed before any real/sensitive tenant data flows
  through the system. Blocks production go-live.
- **P1** — should be closed before general availability, not strictly
  blocking a limited/trusted pilot.
- **P2** — acceptable to defer indefinitely; revisit if it becomes a real
  problem.

## Tracker

| ID | Area | POC shortcut | Production requirement deferred | Priority | Status | Reference |
|----|------|---------------|----------------------------------|----------|--------|-----------|
| OBS-1 | Observability / cost monitoring | Use Langfuse Cloud (hosted SaaS) instead of self-hosting | FR12 / NFR4 — tenant prompts/outputs must not leave the perimeter to a third party | **P0** | Open | [observability-and-cost-monitoring.md](observability-and-cost-monitoring.md) |
| AUDIT-1 | Audit logging | Credential injection outside the agent's view mitigates most in-command secret exposure, but general redaction of secrets a command legitimately prints (env dump, file read, API response) is not solved | FR11 / FR12 — audit trail must never leak a secret, even one the command had legitimate access to | **P1** | Open | [requirements.md](requirements.md) (FR11) |
| AUDIT-2 | Audit logging | Audit store is append-only with no retention period, data-residency handling, or legal-hold support defined | FR11 / NFR9 — compliance-grade audit needs a defined retention policy, residency controls, and legal hold before carrying real compliance-sensitive tenant data | **P1** | Open | [service-architecture.md](service-architecture.md) |
| LOOP-1 | Tenant isolation | The reasoning loop runs as shared, multi-tenant platform code (agent-executor) rather than inside a per-tenant sandbox; a bug in it can affect more than one tenant's session, though it still can't expose a secret | Tenant isolation requirement — a loop bug shouldn't be able to affect another tenant's session at all | **P1** | Open | [service-architecture.md](service-architecture.md) |
| PROXY-1 | Credential injection | agent-network-proxy can inject a credential only for a tool explicitly configured to address the proxy itself as its endpoint (HTTP, MCP, or a CLI like `gh` pointed at it) — the proxy then makes its own outbound call, HTTPS included, with the real credential. It can't inject into traffic addressed straight at the real destination (it can only allow/deny that by the visible destination, e.g. SNI) — by design, not a gap. gRPC could work the explicit-endpoint way in principle but that relay isn't built; raw TCP has no credential slot at all, however it's addressed | Any tool that can't be pointed at the proxy as its endpoint (so stuck doing direct, unconfigured calls), or needs credentialed gRPC/raw TCP, has no supported path; the gRPC relay specifically just needs building | **P1** | Open | [service-architecture.md](service-architecture.md) |

## How to use this

When a POC decision knowingly trades a locked requirement for speed, add a
row here (not just a mention buried in the topic document) so it stays
visible across the whole project rather than only in the document where it
was decided. The topic document keeps the full reasoning; this tracker is
the index of "things that must not be forgotten before production."
