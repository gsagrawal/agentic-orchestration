# Observability & Cost Monitoring — Open Discussion

Status: **OPEN** — not yet discussed in depth.

Referenced from: [requirements.md](requirements.md) §5 (Open Items).

## Why this decision matters

Ties to FR10 (tracing across run → step → tool call → sandbox, plus cost
monitoring), FR12 (never log/persist sensitive data), and NFR12 (cost + scale
calculations as a general practice). The core question: how does anyone —
tenant or operator — know what a given agent is doing *right now*, and what
it has cost so far?

## Questions to resolve

- **Live status visibility**: for a specific `AGENT_RUN`, surface current
  step, current tool call (if any), elapsed time, and — if suspended — what
  it's waiting on (a timer, a human approval, a rate-limit backoff). This is
  as much a query/visibility-API question against the durable-execution
  engine's state as it is a dashboard question.
- **Distributed tracing**: trace ID propagated from agent run → step → LLM
  call / tool call → sandbox, so a single slow or failed run can be followed
  end to end across services.
- **Metrics**: per-tenant throughput, latency, error rate, queue depth,
  sandbox pool utilization, rate-limit rejection rate.
- **Cost monitoring granularity**: per-run and per-tenant breakdown of LLM
  token spend vs. compute spend, attributable back to the `COST_RECORD`
  entity in the ERD — needs to answer "what did this one agent run cost,"
  not just an aggregate bill.
- **Logging redaction**: pairs with FR12 — structured logging with
  field-level redaction so secrets/PII never land in logs, traces, or
  metrics labels, even by accident (e.g. a tool call's raw input/output
  payload).
- **Alerting**: SLA breach, runaway cost on a single run or tenant, stuck/
  stalled agent detection (a run that's neither progressing nor cleanly
  suspended).
- **Tenant-facing vs. internal ops view**: likely the same underlying data,
  but a tenant should only see their own runs/costs, while ops needs the
  cross-tenant view — access-control question on top of the observability
  data model, not a separate pipeline.

## Current thinking: three-layer stack

Started from a concrete doubt: can off-the-shelf tools (an LLM gateway for
cost, Langfuse/Phoenix for observability) actually give per-run cost and live
run status? Answer splits cleanly in two:

- **Per-run cost — yes, but only the LLM half.** LLM gateways (LiteLLM Proxy,
  Portkey, Helicone, Kong AI Gateway) and LLM trace tools (Langfuse, Phoenix)
  both support tagging every call with custom metadata (`tenant_id`,
  `agent_run_id`, `step_id`) and querying/grouping by it, so "total LLM token
  spend for run X" is solved as long as tagging is consistent. Neither covers
  **compute cost** (sandbox CPU-seconds for bash/code-exec tool calls) — that
  has to be metered separately off the sandbox layer and joined at the
  `COST_RECORD` level. Full per-run cost = LLM cost (from the gateway/trace
  tool) + compute cost (from sandbox metering), summed by `run_id`.
- **Live run status — no, not these tools' job.** Langfuse/Phoenix assume a
  trace completes in seconds-to-minutes; they're not built for "this run has
  been suspended for 2 days waiting on a human, since when." That question
  can only be answered by whatever holds the durable state — the
  durable-execution engine itself (Temporal Visibility API / workflow query,
  or the custom `AGENT_RUN`/`STEP` table). Live status is an
  engine-layer concern, decided by
  [durable-execution-engine.md](durable-execution-engine.md), not something
  this discussion can solve independently.

Resulting architecture — three independently-metered layers, joined on
`run_id` / `tenant_id`:

1. **Engine state** (Temporal/custom) → live run status: current step,
   what it's waiting on, since when.
2. **LLM gateway + trace tool**, every call tagged with `run_id`/`tenant_id`
   → LLM reasoning trace + LLM cost.
3. **Sandbox metering**, tagged the same way → compute cost.

Two side notes worth keeping:
- An LLM gateway is also a natural home for the per-tenant rate limiting
  already locked in (FR6) — most do per-key/per-tenant rate limiting
  natively, so it's a two-birds tool, not just a cost tool.
- Given FR12/NFR4 (never log sensitive data; adversarial tenant isolation),
  Langfuse/Phoenix should be **self-hosted** inside the trust boundary, not
  used as SaaS — tenant prompts/outputs shouldn't leave the perimeter to a
  third party.

## Trace tool: Langfuse vs. Phoenix

Both self-hostable (satisfies the self-host requirement above). Trade-off:
- **Phoenix**: OTel-native (OpenInference spec) — vendor-agnostic
  instrumentation, no SDK lock-in, fast single-container setup. Weaker
  out-of-the-box agent-session/cost tooling — originated as an ML-eval tool,
  so per-run cost rollups would largely be hand-built on top of raw spans.
- **Langfuse**: purpose-built for this — native per-trace cost calculation
  from provider pricing tables, and tracing/session UX designed around
  multi-step agent work rather than retrofitted. Heavier self-hosted stack
  (Postgres + ClickHouse + Redis + blob storage) — more setup time, but a
  one-time infra cost. ClickHouse is also the right backend shape for
  aggregating cost queries at the target scale (10k+/sec), so Phoenix would
  likely need similarly heavy storage hardened in before carrying production
  traffic anyway.

**Lean: Langfuse**, self-hosted for production. Not finalized. Regardless of
which is chosen, instrumentation call sites should sit behind a thin
interface of our own (same pattern as the LLM-provider abstraction, NFR6) so
the trace-tool choice isn't a lock-in and isn't a rewrite if revisited later.

### Phasing: POC vs. production

- **POC**: use Langfuse Cloud (the hosted SaaS version) rather than
  self-hosting, to move fast. This is a deliberate, scoped exception to
  FR12/NFR4 (tenant prompts/outputs leaving the perimeter to a third party)
  — acceptable only because the POC is not expected to carry real/sensitive
  tenant data.
- **Production — TODO (P0)**: migrate to self-hosted Langfuse before any
  real tenant data flows through it. Since instrumentation sits behind our
  own thin interface (see above), this should be a backend swap, not a
  re-instrumentation. Tracked as **OBS-1** in
  [poc-vs-production.md](poc-vs-production.md) — see that tracker for the
  full list of POC-phase exceptions across all topics, not just this one.

## Deprioritized (not blocking)

- **Sandbox/compute metering**: real, but LLM spend is expected to dominate
  total cost, so this is parked as a todo rather than a blocker. Depends on
  [sandbox-design.md](sandbox-design.md) once that's discussed.

## Still open

- Specific LLM gateway choice (LiteLLM Proxy vs. Portkey vs. Helicone vs.
  Kong AI Gateway, etc.) — intentionally left open for now
- Final call on Langfuse vs. Phoenix (currently leaning Langfuse, see above)
- Exact shape of the engine-layer Visibility API/query, which depends on the
  still-open durable-execution engine decision
- Per-tenant data isolation model within the self-hosted trace tool (does a
  tenant map to a "project," and is that isolation strong enough given
  NFR3?)
- Live status and audit views need to filter/join by triggering user (FR14),
  not just tenant and agent — the `USER` entity added to the ERD needs to
  flow through to whatever surfaces "what has agent X cost so far."

## Decision log

- 2026-11-24: Settled on the three-layer model (engine state for live status;
  LLM gateway/trace tool for LLM cost+trace; sandbox metering for compute
  cost) joined on `run_id`/`tenant_id`. Tool choice within each layer still
  open.
- 2026-11-24: Sandbox/compute metering deprioritized to a todo — LLM spend
  expected to dominate total cost. LLM gateway choice left intentionally
  open. Leaning Langfuse over Phoenix for the trace tool, not yet finalized
  — see "Trace tool: Langfuse vs. Phoenix" above.
- 2026-11-24: For the POC, use Langfuse Cloud (hosted) instead of
  self-hosting, as a deliberate scoped exception to FR12/NFR4 — POC only,
  not expected to carry sensitive tenant data. TODO before production:
  migrate to self-hosted Langfuse.
