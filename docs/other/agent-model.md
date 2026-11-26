# What Is an Agent? — Open Discussion

Status: **OPEN** — foundational, blocks/revises both the ERD in
[requirements.md](requirements.md) and the lean in
[language-and-framework-choice.md](language-and-framework-choice.md).

Referenced from: [requirements.md](requirements.md) §5 (Open Items).
Indexed as **MC-2** and **MC-4** in [major_calls.md](major_calls.md).

## Why this came up

While discussing implementation language, an assumption slipped in
unchallenged: that adopting Temporal as the durable-execution engine means
an open-source agent framework (LangGraph, CrewAI, AutoGen, etc.) has no
role to play. That's wrong — Temporal governs *reliability* (when a step
runs, how it's retried, how it survives a crash or a days-long wait); it
says nothing about *what the agent's reasoning loop actually is*. Those are
separate axes, and nothing so far rules out an open-source framework for the
second one.

## Candidate shapes for "an agent"

1. **Single LLM, tool loop (ReAct-style)** — call LLM, get a tool-use
   decision, execute, feed the result back, repeat until done. Close to the
   Claude API's native tool-use loop. The current `AGENT_RUN` → `STEP` shape
   in the ERD already models this directly; no graph framework needed.
2. **Graph-based reasoning within one agent** — branching, plan-then-execute,
   self-critique/reflection, parallel sub-tasks. This is where a framework
   like LangGraph's node/edge/state abstraction adds real value over
   hand-coded control flow.
3. **Multi-agent team** — a "run" is actually several cooperating agents
   (supervisor/worker, hierarchical, debate) handing off to each other. If
   in scope, the current ERD does not represent this — `AGENT_RUN`/`STEP`
   would need a parent/child or team structure.

## The composition problem with Temporal

If a framework's own execution engine (e.g. LangGraph's graph runtime, with
its own checkpointer) runs *inside* a single Temporal Activity, two
overlapping durability systems result: Temporal's event history, and the
framework's own persistence. If the framework's internal nodes aren't also
Temporal steps, a crash mid-graph loses whatever progress is inside that one
opaque Activity — quietly undercutting the fine-grained resume FR5 exists
for, unless the framework's checkpointer is wired to the same durable store,
which then raises a split-brain-state problem between two systems each
claiming to know where the run got to.

**Alternative**: don't nest a second stateful engine inside Temporal at all.
Express the reasoning graph's nodes as Temporal steps directly, optionally
borrowing a framework's vocabulary (graphs/nodes/conditional edges) without
adopting its runtime. This also mostly sidesteps the Go-vs-Python ecosystem
question from `language-and-framework-choice.md`, since the dependency at
that point is conceptual, not a runtime dependency on the framework's engine.

## Update after the full problem statement (NFR13, corrected scale)

Checked whether the newer spec-derived requirements (FR13–FR15, trust
levels, NFR13) change this. Most don't: FR13 (credentials never enter the
LLM context), FR14 (attributable to triggering user), FR15 (API vs.
execution tool split), and trust-level isolation all live at the
**tool-dispatch/execution layer**, which behaves identically regardless of
whether a hand-rolled loop or a framework node decided to call the tool —
orthogonal to this question.

Two things do matter:
- **NFR13 (Kubernetes-native-or-justify) strengthens the case against
  nesting a framework's own engine inside Temporal.** Justifying one
  non-native stateful system (Temporal) is already required; composing
  LangGraph's own stateful graph runtime (with its own checkpointer) inside
  a Temporal Activity would be a *second* one, and now requires justifying
  why two systems both claiming to track run progress don't produce the
  split-brain problem described above. Expressing the reasoning graph as
  native Temporal steps avoids that compounding justification burden.
- **The scale correction (NFR1: ~10k/min, not /sec) removes an argument
  against using a Python framework, but doesn't add one for it.** The
  earlier throughput-based push toward hand-rolling (possibly in Go) was
  leaning on the old, wrong number; at ~167/sec (1,670/sec at 10x) a
  framework's overhead isn't a bottleneck. This just neutralizes a prior
  objection — the composition/correctness problem above is the actual
  deciding factor, and it's unaffected by scale.

Net: these details sharpen *why* native-Temporal-steps is favored over a
nested framework engine, but don't resolve *which agent shape* (1/2/3 below)
is actually needed — that's still the open question that actually matters.

## Scope decision: POC shape vs. future direction

- **POC**: shape 1 (single agent, simple tool loop) — assumed default,
  pending confirmation if branching/reflection logic (shape 2) is actually
  wanted in v1 too.
- **Future direction**: multi-agent (shape 3) is the intended destination,
  explicitly not required in the initial POC.
- **Why this doesn't require speculative architecture now**: since
  `AGENT_RUN` maps to one durable-execution-engine workflow instance (see
  [durable-execution-engine.md](durable-execution-engine.md)), multi-agent
  composition is just a parent workflow spawning/coordinating child
  `AGENT_RUN` workflows — Temporal's native Child Workflow primitive is
  built for exactly this. The only change needed now is additive: a
  nullable `parent_run_id` on `AGENT_RUN` (added to the ERD) that the POC
  never populates, plus a future `STEP` type ("spawn child agent run" /
  "await child agent run") alongside `LLM_CALL` and `TOOL_CALL`. No redesign
  required later.
- **This also closes the LangGraph-vs-custom question for the multi-agent
  case specifically**: multi-agent support doesn't make a case for adopting
  a framework's own multi-agent coordination patterns (e.g. LangGraph's
  supervisor pattern), because Temporal's Child Workflow primitive already
  provides that composition mechanism. Adopting a framework for it later
  would mean building the same coordination twice — the same
  nested-stateful-engine problem already flagged under NFR13, just arriving
  through the multi-agent door instead of the single-agent one.

## Resolved: durability ownership (platform vs. agent)

See [assumptions.md](assumptions.md) for the full argument. Resolved:
**platform-owned**, via a replayable event log at the tool-call boundary —
not agent-owned. An agent-internal checkpoint (e.g. a framework's own, like
LangGraph's) may still exist as a non-authoritative performance cache, but
is never the source of truth; on resume, the platform replays its own
tool-call history back into the agent. This directly resolves the
split-brain concern raised earlier in this document. Accepted cost: a crash
mid-internal-reasoning (not at a tool-call boundary) means redoing that
reasoning, not resuming it — traded for uniformity across agent shapes.

## Resolved: multi-agent representation

See [assumptions.md](assumptions.md). An agent-to-agent call is a
`TOOL_CALL` with `TOOL_DEFINITION.category = "agent"` — not a bespoke `STEP`
type — going through the same authorization/credential/audit/rate-limit
path as any other tool call. `AGENT_RUN.parent_run_id` is set by the gateway
when it creates the child run.

## Remaining open questions

- Which shape (1, 2, or 3) is actually needed, now and foreseeably? (POC =
  shape 1, per the earlier scope decision above.)
- If (2): compose an existing framework's engine inside the agent process
  (now explicitly *inside* the opaque-agent boundary, invisible to the
  platform either way — the durability resolution above means this is a
  lower-stakes choice than previously framed, since the platform doesn't
  depend on it for correctness), or express it as the agent's own
  hand-rolled logic?
- Does this change the lean in
  [language-and-framework-choice.md](language-and-framework-choice.md)? Less
  than previously thought — the opaque-agent assumption means an
  agent-internal framework choice (if any) doesn't force a platform-wide
  language choice; only the platform's own control-plane language does.

## Decision log

- 2026-11-25: POC targets shape 1 (single agent, simple tool loop); shape 3
  (multi-agent) confirmed as the future direction, deliberately out of
  scope for the POC. Architecture kept compatible via an additive
  `parent_run_id` on `AGENT_RUN` rather than any speculative build-out now.
