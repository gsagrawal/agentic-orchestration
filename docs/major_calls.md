# Major Architectural Calls

Register of the big, hard-to-reverse decisions — for fast skimming. Full
reasoning lives in the linked topic document; this is the index and the
verdict, not a duplicate of the analysis.

Referenced from: [requirements.md](requirements.md).

## MC-1: Production execution engine — Temporal vs. custom message-queue vs. LangGraph-as-engine

**Status: OPEN, leaning Temporal.**
Full detail: [durable-execution-engine.md](durable-execution-engine.md),
[agent-model.md](agent-model.md).

Three real options:
1. **Temporal (self-hosted)** — purpose-built for "an arbitrary long-running
   process survives infrastructure failure" (pod death, deploy, node drain)
   — which is this project's literal hard requirement (FR4/FR5/NFR5).
   Production-hardened at high-stakes multi-tenant scale.
2. **Custom-built** (message queue + DB) — full control, highest
   engineering cost/risk (see the 7-component breakdown in
   durable-execution-engine.md).
3. **LangGraph-as-engine** — using LangGraph's own checkpointer/runtime (and,
   for distributed/scaled execution, LangGraph Platform) as the durable
   substrate itself, not just a reasoning-definition layer. Has real
   durability features (per-super-step checkpointing, `interrupt()` for
   human-in-the-loop) that map directly onto FR4. But durability is a
   secondary feature of a tool whose core purpose is defining agent
   reasoning graphs, not a hardened general-purpose failure-survival
   substrate — and the production-grade distributed/horizontally-scaled
   story leans more on the commercial Platform than the self-hosted OSS
   checkpointer (worth re-verifying current state before finalizing; this
   space moves fast). Would also reopen and likely reverse the Go lean in
   [language-and-framework-choice.md](language-and-framework-choice.md),
   since LangGraph is Python-first — a cascading consequence, not an
   isolated choice.

**Verdict**: Temporal remains the stronger choice for the engine, because
its core design purpose is exactly this project's core hard requirement,
with a production track record LangGraph's durability story doesn't yet
match at this scale/tenancy bar. Custom-build remains the second real
option on engineering-control grounds, pending the cost/scale calculation
(NFR12). LangGraph-as-engine is not selected.

**Reversibility: hard.** Every other component (agent model, state store,
observability wiring, sandbox dispatch) assumes one durable engine exists —
switching later is a re-platform, not a refactor.

**Reinforced by [assumptions.md](assumptions.md)**: the platform-owned,
replayable-event-log durability argued for there has a direct, concrete
realization in Temporal's own workflow event history — this isn't a
separate mechanism to build, it's the same one.

## MC-2: Agent reasoning layer — LangGraph (as a library) vs. own setup (native engine steps)

**Status: de-risked — no longer a platform-wide architectural commitment.**
Full detail: [agent-model.md](agent-model.md), [assumptions.md](assumptions.md).

Originally framed as whether the agent's reasoning control flow is
expressed via a framework's own execution engine composed inside Temporal,
or natively as Temporal steps. The opaque-agent assumption changes the
stakes: the platform only ever sees tool calls, and durability is owned by
the platform at that boundary regardless of what happens inside an agent.
So whatever an agent uses internally (LangGraph, hand-rolled logic, or
anything else) is now a **per-agent implementation detail**, invisible to
and independent of the platform's architecture — not a platform-wide
commitment the way MC-1 is.

**Verdict**: no platform-level choice needs to be forced here. An agent
implementation may use LangGraph internally if it wants to; the platform
doesn't need to know or care, as long as the agent only crosses the
platform boundary via tool calls and accepts replay of its own tool-call
history on resume.

**Reversibility: N/A at the platform level** — this is now scoped per
agent, not a platform architecture decision to reverse.

## MC-3: Implementation language — Go vs. Python vs. polyglot

**Status: leaning Go, contingent on MC-1 and MC-2.**
Full detail: [language-and-framework-choice.md](language-and-framework-choice.md).

Leaning Go for the control plane given the Temporal-Go SDK fit and the
throughput/footprint profile (though the corrected scale, NFR1, lowers the
pressure this argument carries). Explicitly contingent: if MC-1 or MC-2 had
resolved toward LangGraph, this would need to flip toward Python.

**Reversibility: hard** once significant code exists, cheap right now.

## MC-4: Multi-agent future-readiness

**Status: decided.**
Full detail: [agent-model.md](agent-model.md).

POC targets a single-agent tool loop; multi-agent is the stated future
direction. Resolved as an additive-only change — a nullable `parent_run_id`
on `AGENT_RUN` — rather than speculative build-out now, since multi-agent
composition is expected to map onto Temporal's Child Workflow primitive
once needed.

**Reversibility: N/A** — additive, not a constraint that could be "wrong."

## Decision log

- 2026-11-25: Register created. Seeded with MC-1 through MC-4 from prior
  discussion. MC-1 reopened to explicitly evaluate LangGraph as a candidate
  *engine* (not just reasoning layer) — verdict stays with Temporal.
- 2026-11-25: Core assumptions captured in [assumptions.md](assumptions.md)
  (opaque agent, platform-owned durability, multi-agent as a tool call).
  MC-1 reinforced (Temporal's event history realizes the platform-owned
  durability argument directly). MC-2 de-risked to a per-agent
  implementation detail, no longer a platform-wide commitment.
