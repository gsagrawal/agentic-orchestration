# Language & Framework Choice — Open Discussion

Status: **OPEN** — leaning Go for the control plane, final call not made.

Referenced from: [requirements.md](requirements.md) §5 (Open Items).

## Why this decision matters

This is the implementation language for the orchestration framework itself
(scheduler, rate limiter, sandbox lifecycle manager, Temporal workers) —
distinct from the language tenant-submitted code runs in inside the
code-execution sandbox, which is unrelated to this choice.

## The doubt that started this

Go has a small runtime footprint, attractive for NFR1 (10k+/sec peak, 10x
headroom) and NFR8 (cost efficiency), but the "agent infra" ecosystem
(LangChain/LangGraph/CrewAI-style frameworks, RAG/eval tooling) is
overwhelmingly Python-first. Python is the default language choice for LLM
work generally.

## Analysis

**Go is better-supported here than the doubt suggests:**
- Temporal (the leading durable-execution candidate, see
  [durable-execution-engine.md](durable-execution-engine.md)) was built in
  Go; its Go SDK is its most mature, most idiomatic SDK.
- Anthropic publishes an official Go SDK alongside Python/TypeScript.
- MCP has Go SDK support, though Python/TypeScript have more existing
  third-party MCP servers if we integrate tools that way.

**Why the Python ecosystem gap matters less than usual here:** the main
value of Python agent frameworks is pre-built orchestration (tool-dispatch
loops, memory, multi-agent coordination) — exactly the layer this project
is building itself on top of Temporal + its own ERD, not outsourcing to a
framework. What's left to implement directly — call the LLM, parse tool-use
output, feed results back — is a small loop in either language, especially
behind a single-provider abstraction that's already being hand-rolled
(NFR6).

**Where Go has a direct advantage for this system specifically:** this is a
high-throughput, I/O-bound control plane (10k+/sec tool calls, thousands of
concurrent in-flight LLM/tool waits) — close to Go's goroutine sweet spot.
Smaller memory footprint and faster cold-start than Python support denser
worker packing and faster scale-up under the bursty load profile already
noted. Also a secondary security point: the orchestrator touches every
tenant's credentials (FR3), so a smaller, statically-linked trusted
computing base is a modest plus over Python's larger interpreter +
dependency surface.

**Where Python would still win:** if the team wants to lean on its broader
RAG/embeddings/eval ecosystem for specific agent capabilities, or if team
familiarity skews Python (a real velocity factor, not just a technical one).

**Middle path worth keeping in mind:** Temporal supports polyglot workers
against the same task queues — Go for the control plane, with Python usable
for a specific Activity implementation if a capability genuinely needs that
ecosystem, without forcing an all-or-nothing choice.

## Current lean

Leaning **Go** for the control plane (scheduler, rate limiter, sandbox
lifecycle, Temporal workers), given the Temporal-Go fit and the
throughput/footprint profile. Not finalized — open to a split/polyglot
approach if specific capabilities warrant Python.

- **Depends on [agent-model.md](agent-model.md)**: the ecosystem-gap argument
  above assumed away the question of whether an open-source agent framework's
  own execution engine (e.g. LangGraph) gets composed into this system at
  all. That assumption was wrong — it's a genuinely open question, not
  settled by picking Temporal. If a framework's engine *is* adopted, the
  language choice may be constrained by that framework's own
  language/ecosystem rather than decided independently.

## Still open

- Final single-language vs. polyglot (Go control plane + Python activities)
  decision
- Team familiarity / hiring constraints, which weren't factored in yet
- Whether any planned capability (RAG, evals, etc.) specifically needs
  Python's ecosystem strongly enough to justify polyglot complexity

## Decision log

- 2026-11-24: Leaning Go for the control plane; Python's ecosystem advantage
  judged to matter less here since this project builds its own
  orchestration layer rather than consuming a pre-built agent framework.
  Not finalized.
