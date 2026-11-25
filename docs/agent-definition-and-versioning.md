# Agent Definition & Versioning — Open Discussion

Status: **OPEN** — not yet discussed. One of the items the spec explicitly
requires us to state (see [problem-statement.md](problem-statement.md)).

Referenced from: [requirements.md](requirements.md) §5 (Open Items). Closely
tied to [agent-model.md](agent-model.md) (what an agent's reasoning loop
*is*) and to the workflow-versioning concern already flagged in
[durable-execution-engine.md](durable-execution-engine.md) (item 6) and
[durability-and-resilience.md](durability-and-resilience.md).

## Why this decision matters

`AGENT_DEFINITION` in the ERD currently has `name`, `allowed_tools`,
`model_config` — but nothing about how a definition changes over time while
`AGENT_RUN`s created from an older version are still mid-flight, some
possibly suspended for days waiting on a human. This is the same versioning
problem flagged elsewhere, but concretely scoped to "what does an agent
definition even look like, and what happens when it changes."

## Questions to resolve

- **What's actually in a definition?** System prompt, tool grant list
  (which ties directly to FR13/FR15 — an agent can only call tools it was
  granted), model/model-config, trust level (per the spec's "different...
  trust levels" — does trust level live on the definition or somewhere
  tenant-level?), owner.
- **Versioning scheme**: immutable versions (each edit creates a new
  version, old `AGENT_RUN`s keep referencing the version they started on)
  vs. mutable-with-migration (in-flight runs get upgraded, requiring a
  compatibility/migration story). Immutable versions are the safer default
  given FR5/NFR5 (a run resuming after days shouldn't suddenly behave
  differently because its definition changed underneath it) — but not yet
  decided.
- **Where definitions live**: part of the state store (see
  [state-store.md](state-store.md)) as data, vs. defined as code/config
  deployed alongside the orchestrator itself (closer to how Temporal
  workflow code versioning normally works, via the engine's own versioning
  primitives rather than a database row).
- **Tool-grant enforcement**: FR13/FR15 require that an agent cannot call a
  tool it wasn't granted — this needs to be enforced at the dispatch layer
  against the definition's grant list, not just declared in the definition
  and trusted.
- **Relationship to the durable-execution engine's own versioning**: if
  Temporal is adopted, does "agent definition version" map onto Temporal's
  own workflow-versioning mechanism, or is it a separate, parallel concept
  that has to be kept in sync with it?

## Decision log

(none yet)
