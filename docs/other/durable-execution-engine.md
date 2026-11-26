# Durable Execution Engine — Decision Record (Working)

Status: **OPEN** — leaning toward option 1 or option 4 below, final call deferred
to a cost + data-scale calculation (tracked as NFR12 in [requirements.md](requirements.md)).

Referenced from: [requirements.md](requirements.md) §4 (ERD) and §5 (Open Items).
Indexed as **MC-1** in [major_calls.md](major_calls.md).

## Kubernetes-native-or-justify (NFR13)

Per [problem-statement.md](problem-statement.md), any choice that isn't a
native K8s primitive must be explicitly justified. A workflow engine
(Temporal or custom) is not itself a K8s primitive — the justification: K8s
has no native primitive for a task that must durably survive for days
across pod restarts/deploys while waiting on an external signal (a human
reply). K8s Jobs/CronJobs handle retryable batch work, not this. The engine
(whichever one) still runs *on* K8s as ordinary deployments/pods — scheduling,
scaling, and node-level failure handling for the engine's own workers are
still K8s-native; what's not K8s-native is the durable-state/signal
semantics the engine adds on top.

## Why this decision matters

This is the engine behind `AGENT_RUN` / `STEP` in the ERD. It's what makes
FR4 (tool calls from seconds to days), FR5 (resume after pod death), and FR8
(a waiting agent costs ~0 compute) actually true, at the throughput in NFR1/NFR2
and with the portability constraint in NFR7 (K8s-native now, on-prem later,
avoid single-cloud dependency).

## Options considered

### 1. Temporal (self-hosted OSS) — leading candidate
Workflow = `AGENT_RUN`, Activity = `STEP`. Built-in retries, timeouts,
heartbeats, and Signals (maps directly onto `HUMAN_APPROVAL_REQUEST` — a
workflow parked for days at zero compute cost). Proven at throughput well
past 10k/sec (Stripe, Netflix, Snap). Runs on our own K8s cluster — no cloud
lock-in, portable to on-prem later. Cost is primarily ops overhead: running
the Temporal server (frontend/history/matching + a persistence store,
Cassandra or Postgres) and a worker fleet, plus the determinism constraint on
workflow code (non-deterministic work — LLM/tool calls — must be Activities,
which is natural here anyway).

### 2. AWS Step Functions — rejected
Serverless, pay-per-transition, native long-callback support. **Ruled out**:
ties the orchestration control plane to a single cloud provider, directly
conflicting with the portability goal (NFR7) and the explicit decision to
avoid single-cloud dependency.

### 3. Lighter/newer engines (Restate, DBOS, etc.) — needs deeper investigation
Not yet ruled in or out. Restate (journal-based durable execution) and DBOS
(durability embedded in Postgres transactions) are both self-hostable
(NFR7-compatible) with lower ops overhead than Temporal, but with far less
production track record at our scale class. Before considering further:
evaluate production maturity, scale evidence against NFR1/NFR2, and
community/support depth.

### 4. Custom-built (event log + queue + DB) — leading candidate
What this actually requires building — effectively Temporal's core,
reimplemented:
1. Append-only event log for `STEP` state
2. A durable timer service that survives restarts and fires reliably days
   later (not a cron job — a real persisted-timer system)
3. Retry/backoff policy engine
4. Worker lease + heartbeat, to detect dead workers and reassign in-flight work
5. Idempotency handling so a resumed step can't double-execute a tool call
6. Versioning strategy for workflows whose code changes while instances are
   still mid-flight
7. Visibility/debug tooling to list and inspect running agent runs

Items 5 and 6 are the ones most likely to quietly consume months — exactly
where Temporal has years of production hardening. Full control, zero vendor
dependency, higher engineering cost and delivery risk.

### Temporal Cloud (managed) — parking lot, not primary
Fallback if self-hosted Temporal's ops burden becomes the real blocker.
Removes ops overhead but reintroduces a vendor dependency and a
per-action billing model — worth its own look only if self-hosting proves
painful in practice.

## Current lean

Leaning toward **option 1 (Temporal, self-hosted)** or **option 4
(custom-built)**. AWS Step Functions excluded on single-cloud-dependency
grounds. Option 3 deferred pending deeper research. Final choice between 1
and 4 deferred to the cost + data-scale calculation below.

## Next step: cost + data-scale calculation

Tracked as NFR12 in requirements.md. Needs, side by side for Temporal
(self-hosted) vs. custom-build:
- Infra cost at 10k+ tool calls/sec peak, with 10x headroom (NFR1)
- Event-history / state storage growth under projected throughput and
  retention assumptions
- For Temporal: persistence store + worker fleet sizing
- For custom-build: engineering time estimate for the 7 components above,
  plus ongoing infra cost once built
- Output: a side-by-side cost table and a recommendation

## Decision log

- 2026-11-24: AWS Step Functions ruled out — single-cloud dependency
  conflicts with the portability goal.
- 2026-11-24: Restate/DBOS deferred pending deeper investigation.
- 2026-11-24: Leaning Temporal (self-hosted) or custom-build; final call
  deferred to the cost + data-scale calculation.
