# Core Assumptions

Status: **foundational** — these are premises the design proceeds from, not
decisions to re-litigate like [major_calls.md](major_calls.md). Given as
framing, with two explicit resolutions argued below.

Referenced from: [requirements.md](requirements.md). Resolves open questions
in [agent-model.md](agent-model.md).

## The core assumption: agents are opaque units on the tool-call path

The platform treats every agent as an opaque unit that emits tool calls and
consumes results. Isolation, authorization, credential injection, audit
logging, durability, and rate limiting all attach to **the tool-call path**
— never to whatever the agent does internally to decide its next tool call.
This is what makes one design work across heterogeneous agent shapes: one
enforcement/durability/audit mechanism, anchored at one boundary, regardless
of how sophisticated or different the agents behind it are.

## Three reference shapes

1. **Single tool-loop (reference shape)**: model call → tool call → result
   → repeat. The POC uses this shape, with a **scripted fake model**
   (deterministic, not a real LLM) so the orchestration plumbing — dispatch,
   durability, audit, rate limiting — can be tested without LLM cost or
   nondeterminism in the loop.
2. **Graph-reasoning agent**: an internal workflow inside *one* agent
   (branching, planning, reflection). The platform sees only its tool calls
   — the graph's internal nodes/transitions are invisible to the platform.
3. **Multi-agent team**: any agent-to-agent call is itself a tool call
   through the same gateway (see below) — not a separate primitive.

## Resolved: durability is owned by the platform, not the agent

**Platform-owned, via a replayable event log at the tool-call boundary.**

1. **Uniformity**: agent-owned durability would mean every agent shape needs
   its own mechanism (the loop needs one, a LangGraph-based graph needs
   LangGraph's checkpointer, an AutoGen-based team needs a third) — exactly
   what the opaque-agent assumption exists to avoid. One platform-owned
   mechanism, trusted once, beats N agent-owned ones, each trusted separately.
2. **Already required**: FR11 (command-level audit) and FR14 (attribution)
   already force an authoritative, queryable record of every tool call —
   that record *is* the resumability substrate. No second system to build.
3. **The spec's phrasing is platform-centric**: "an agent mid-task survives
   a pod restart" describes a platform guarantee, not something each
   heterogeneous agent *owner* is trusted to implement correctly.
4. **Resolves the split-brain risk** flagged in `agent-model.md`: an
   agent-internal checkpoint (e.g. LangGraph's own) is explicitly a
   non-authoritative cache, never a second source of truth. On resume, the
   platform replays its own tool-call history back into the agent to
   reconstruct context; an internal checkpoint only avoids recomputing
   internal reasoning, and is never required for correctness.

**Accepted cost**: a crash mid-internal-reasoning (not at a tool-call
boundary) means redoing that reasoning from the last tool-call result, not
from wherever it was — a real cost given LLM calls are the largest cost
line, but the price of uniformity. An agent may still keep its own internal
checkpoint purely as a performance optimization on top of this; it is never
the authority.

## Resolved: multi-agent calls are tool calls, not a new primitive

An agent-to-agent call is a `TOOL_CALL` whose `TOOL_DEFINITION.category` is
`agent` — a third value alongside `api`/`execution` — carrying its own
identity, tenant, and audit trail, and going through the **exact same**
authorization check, credential handling, audit log, and rate limit as any
other tool call. The gateway sets the child's `parent_run_id` (added to the
ERD in [requirements.md](requirements.md)) when it creates the child run in
response to this call. No side channel: there is no second code path that
could skip an authorization check for agent-to-agent calls specifically.

## Consequences elsewhere

- [agent-model.md](agent-model.md): durability-ownership question resolved
  (platform-owned); its decision log updated accordingly.
- [requirements.md](requirements.md) ERD: `TOOL_DEFINITION.category` gains
  `agent` as a third value; multi-agent composition needs no bespoke `STEP`
  type — just a `TOOL_CALL` targeting another agent.
- [major_calls.md](major_calls.md) MC-2 (reasoning layer): reinforced, not
  changed — native-steps-vs-framework-engine is now specifically a question
  about what happens *inside* the opaque-agent boundary, which the platform
  never needs to see.

## Decision log

- 2026-11-25: Assumptions captured as given. Durability resolved as
  platform-owned (replayable event log at the tool-call boundary).
  Multi-agent resolved as tool-call-through-the-same-gateway, no side
  channel.
