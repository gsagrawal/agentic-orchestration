# Core Assumptions

Status: **foundational** — these are premises the design proceeds from, not
decisions to re-litigate like [major_calls.md](major_calls.md). Given as
framing, with two explicit resolutions argued below.

Referenced from: [requirements.md](requirements.md). Resolves open questions
in [agent-model.md](agent-model.md).

## The core assumption: agents are opaque for durability and bookkeeping only

**The platform treats agents as opaque for durability and bookkeeping
only, with two mandatory choke points — tool calls and LLM calls — and a
fully sandboxed agent process with no other egress.**

Two boundaries, not one:
- **Tool-call boundary**: authorization, credential injection, audit
  logging, and durability/resumability attach here. This is where the
  platform's replayable event log lives.
- **LLM-call boundary**: every model call, from any agent shape — including
  internal planning/reflection calls inside a graph-reasoning agent that
  never become a tool call — is routed through the platform's LLM-call
  gateway. This is where rate limiting and cost metering attach, and it
  exists independently of whether a given model call corresponds to a
  platform-checkpointed step.
- **No third path**: the agent process itself runs fully sandboxed with no
  network egress other than these two choke points. This is what makes
  "opaque" safe rather than a blind spot — there is no channel through
  which an agent could take a side-effecting or data-exfiltrating action,
  or spend LLM budget, without passing through an enforced, audited point.

"Opaque" describes the platform's *durability/bookkeeping* model — it does
not checkpoint or model an agent's internal reasoning steps as durable
state. It does not describe the *security* model: the sandbox and the
LLM-call gateway see and constrain everything, always, regardless of agent
shape.

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

**Why redo-on-crash is safe, not just wasteful**: this only holds because
of the no-third-path guarantee above — non-tool-call agent activity is
*guaranteed* to have zero external side effects, since any side-effecting
action is required to be a credentialed tool call. Redoing internal
reasoning therefore only burns compute/LLM cost, never duplicates a
real-world effect. This safety property depends on the sandbox enforcing
"no egress outside the two choke points" as an actual invariant, not a
convention agents are expected to follow.

## Resolved: multi-agent calls are tool calls, not a new primitive

An agent-to-agent call is a `TOOL_CALL` whose `TOOL_DEFINITION.category` is
`agent` — a third value alongside `api`/`execution` — carrying its own
identity, tenant, and audit trail, and going through the **exact same**
authorization check, credential handling, audit log, and rate limit as any
other tool call. The gateway sets the child's `parent_run_id` (added to the
ERD in [requirements.md](requirements.md)) when it creates the child run in
response to this call. No side channel: there is no second code path that
could skip an authorization check for agent-to-agent calls specifically.

## Stress-test: why the original one-boundary framing didn't survive

The first draft of this assumption said isolation/authz/credential/audit/
durability/rate-limiting all attach to "the tool-call path." That
contradicted an already-locked requirement: LLM-call rate limiting (FR6)
and the LLM gateway exist because the LLM provider is explicitly "external,
rate-limited, and the single largest cost line." A graph-reasoning agent's
*internal* model calls never become tool calls — so a tool-call-only
boundary makes those calls invisible to the very mechanism meant to
rate-limit and meter them. Same gap undermines "what is agent X doing right
now" (FR10/observability hard requirement): a fully opaque agent gives the
platform nothing to distinguish "reasoning" from "stuck" between tool calls.
Fixed by splitting into two mandatory choke points instead of one, with the
sandbox closing the gap between them (no third path to do anything,
anywhere, unobserved).

## Consequences elsewhere

- [agent-model.md](agent-model.md): durability-ownership question resolved
  (platform-owned); its decision log updated accordingly.
- [requirements.md](requirements.md) ERD: `TOOL_DEFINITION.category` gains
  `agent` as a third value; multi-agent composition needs no bespoke `STEP`
  type — just a `TOOL_CALL` targeting another agent. `LLM_CALL` now attaches
  primarily to `AGENT_RUN` (always present, via the LLM-call gateway), with
  `STEP` as an optional secondary link only where a model call happens to
  double as a durability checkpoint (the reference shape).
- [requirements.md](requirements.md) FR list: new FR16 (every model call
  routes through the LLM-call gateway, regardless of agent shape) and FR17
  (agent process is fully sandboxed, no egress outside the two choke
  points).
- [sandbox-design.md](sandbox-design.md): FR17 is now a decided
  requirement on *what* the sandbox must guarantee (no egress outside the
  two choke points); *which* sandbox technology delivers that is still open.
- [observability-and-cost-monitoring.md](observability-and-cost-monitoring.md):
  the LLM-call gateway gives a cheap liveness signal ("last model call N
  seconds ago") for live-status views, even during an opaque agent's
  internal-only phases.
- [major_calls.md](major_calls.md) MC-2 (reasoning layer): reinforced, not
  changed — native-steps-vs-framework-engine is now specifically a question
  about what happens *inside* the opaque-agent boundary, which the platform
  never needs to see.
- [agent-runtime.md](agent-runtime.md) (MC-5): this document states the
  two-choke-point contract as a principle; agent-runtime.md is where it's
  made real in code (sandbox egress policy, LLM proxy, MCP tool gateway).

## Decision log

- 2026-11-25: Assumptions captured as given. Durability resolved as
  platform-owned (replayable event log at the tool-call boundary).
  Multi-agent resolved as tool-call-through-the-same-gateway, no side
  channel.
- 2026-11-25: Challenged and corrected the one-boundary framing — rate
  limiting and observability both require a second mandatory choke point
  (LLM-call gateway) independent of the tool-call/durability boundary, and
  "opaque" was clarified to mean the durability/bookkeeping model only, not
  the security model — the sandbox still sees and constrains everything via
  "no egress outside the two choke points."
