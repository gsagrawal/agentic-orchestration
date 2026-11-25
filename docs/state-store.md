# State Store — Open Discussion

Status: **OPEN** — not yet discussed. One of the items the spec explicitly
requires us to state (see [problem-statement.md](problem-statement.md)).

Referenced from: [requirements.md](requirements.md) §5 (Open Items).

## Why this decision matters

Every entity in the ERD (`AGENT_RUN`, `STEP`, `COST_RECORD`,
`AUDIT_LOG_ENTRY`, etc.) has to actually live somewhere. This is also not
independent of the durable-execution engine choice
([durable-execution-engine.md](durable-execution-engine.md)): if Temporal is
adopted, Temporal itself needs a persistence store (Cassandra or Postgres),
and the question becomes whether our own application-level state (the ERD
entities not owned by Temporal's own event history, e.g. `COST_RECORD`,
`AUDIT_LOG_ENTRY`, `CREDENTIAL`) shares that store or uses a separate one.

## Questions to resolve

- Does the state store need to be one system, or is it legitimately split —
  e.g. Temporal's own persistence store (whatever Temporal needs) plus a
  separate relational store for the rest of the ERD?
- Candidate(s) for the relational/application-state side: Postgres (default,
  well-understood, fine at the corrected scale in NFR1) vs. something else —
  given the corrected ~167/sec peak (NFR1), there's no longer a strong case
  for an exotic/distributed store on throughput grounds alone.
- Multi-tenancy at the storage layer: shared tables with a `tenant_id`
  column and row-level security, vs. schema-per-tenant, vs. database-per-tenant
  — trade-off between isolation strength (NFR3) and operational overhead at
  ~1000 tenants' worth of agents.
- Retention and compliance implications (ties to FR11/NFR9 — audit log
  retention) on whatever store ends up holding `AUDIT_LOG_ENTRY`.
- Where `CREDENTIAL.secret_ref` points to (a dedicated secret store like
  Vault/KMS) is a related but distinct question — this doc is about the
  state store for the ERD's operational/business entities, not the secret
  store itself.

## Decision log

(none yet)
