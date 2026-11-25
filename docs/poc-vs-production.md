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

## How to use this

When a POC decision knowingly trades a locked requirement for speed, add a
row here (not just a mention buried in the topic document) so it stays
visible across the whole project rather than only in the document where it
was decided. The topic document keeps the full reasoning; this tracker is
the index of "things that must not be forgotten before production."
