# Agent Runtime / Integration Surface — Open Discussion

Status: **OPEN**, newly identified, with a strong lean. Indexed as **MC-5**
in [major_calls.md](major_calls.md).

Referenced from: [requirements.md](requirements.md) §5 (Open Items).
Realizes the two-choke-point contract from [assumptions.md](assumptions.md)
at the code/infrastructure level.

## Not to be confused with: overall agent management (MC-1 + MC-2)

This document is **not** about who owns crash survival or whether an
agent's internal shape matters to the platform — that's already fully
settled: Temporal owns durability (MC-1), and an agent's internal shape
(plain loop or LangGraph) is irrelevant to the platform either way (MC-2).
If the platform didn't use Temporal (custom message-queue build instead),
*that* durability/replay machinery would need to be built regardless of
agent shape — but that's a restatement of MC-1/MC-2, not a reason to read
anything here. This document is about a separate, narrower question that
exists independently of the engine choice: once an agent wants to make a
tool call or an LLM call, what mechanism actually gets it there under
enforcement? Keep these two discussions separate.

## Why this came up

Re-examining "LangGraph vs. Temporal" surfaced that it was a category
error: Temporal is the platform engine (MC-1, survives crashes); LangGraph,
if used, is at most something *inside* one agent's opaque boundary (MC-2,
the platform never sees it). Neither answers how an agent's internal code
actually reaches a tool or a model without going around the two choke
points from `assumptions.md`. That's a third piece, previously unnamed:
whatever makes "every tool call via the tool-call gateway, every model call
via the LLM-call gateway" true in code, not just true as a stated
principle.

## Lean: infrastructure enforcement + standard protocols, not a bespoke SDK

This can be mostly solved without a custom runtime, by making both choke
points things any agent framework already knows how to plug into:

- **LLM-call boundary**: sandbox network policy allows egress to nothing
  except a platform proxy address (ties to FR17/sandbox-design.md). Every
  LLM client library already supports a custom `base_url` — pointing at the
  proxy is a config change, not code the platform writes per agent. The
  proxy enforces rate limiting (FR6/FR16) and cost metering; the agent
  doesn't know it isn't talking to the real provider directly.
- **Tool-call boundary**: expose tools via **MCP** — the platform's gateway
  acts as an MCP server, filtered to exactly the tools that
  agent/tenant/user combination is granted (FR15's tool-grant enforcement),
  handling authorization, credential injection (FR13), and audit logging
  (FR11/FR14) server-side. Any framework that speaks MCP as a client
  (LangGraph does; most current agent frameworks do) gets enforcement by
  pointing at the gateway's endpoint — configuration, not a runtime.

Under this lean, "LangGraph vs. hand-rolled loop" plug into the *same* two
integration points the same way — one proxy + one MCP gateway, built once,
not a per-framework SDK.

## What's still genuinely ours to build: resume/replay hydration

When the durable engine restarts an agent's Activity after a crash
(FR5/NFR5), something has to feed the prior tool-call history back in so
the agent reconstructs context before continuing — this is the concrete
mechanism behind the "accepted cost" in `assumptions.md`. For the reference
single-loop shape this is trivial (replay messages into the next model
call). For a LangGraph-based agent, LangGraph already accepts prior
state/messages on invocation, so this is a thin adapter, not a rewrite.
Naturally scoped per-agent or per-framework-adapter — not a monolithic
runtime built once for every possible agent shape.

## Resolved

- **Tool-call protocol for the POC**: plain HTTP/gRPC, not MCP. Simpler to
  stand up for the POC's reference shape; MCP noted as a compatible future
  upgrade (the gateway's contract shouldn't be designed in a way that rules
  out exposing an MCP-compatible interface later, but doesn't need to
  provide one now).
- **Gateway placement**: a shared platform-level service, not a sidecar per
  sandbox. Sidecar noted as an alternative worth remembering (e.g. if
  blast-radius or per-tenant throughput isolation become a concern later),
  not chosen now.

## Questions to resolve

- What exactly is the resume/replay hydration contract — a documented
  function signature agent owners implement, or a reference adapter the
  platform provides for the reference shape and maybe LangGraph, with
  other frameworks left to their owners?
- Does this interact with agent-definition versioning
  ([agent-definition-and-versioning.md](agent-definition-and-versioning.md))
  — the integration-surface contract (proxy/MCP protocol version) and the
  agent definition version are two different things that can each change
  independently; worth being explicit they don't get conflated.

## Decision log

- 2026-11-25: Identified as a distinct concern from MC-1/MC-2. Leaning
  toward infrastructure enforcement (sandbox egress policy + LLM proxy +
  MCP tool gateway) over a bespoke per-framework SDK, specifically because
  it keeps the "any agent shape, including LangGraph-based" property true
  without extra engineering per framework. Resume/replay hydration
  identified as the one piece still genuinely custom, and scoped as
  per-agent/per-adapter rather than a shared runtime.
- 2026-11-25: POC tool-call protocol settled as plain HTTP/gRPC (MCP noted
  compatible, not required now). Gateway placement settled as a shared
  platform-level service (sidecar noted as an alternative, not chosen).
