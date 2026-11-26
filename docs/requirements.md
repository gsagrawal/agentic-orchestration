See also: [problem-statement.md](problem-statement.md) — the verbatim source
spec. This document tracks it; where they diverge, this document is wrong.
See also: [service-architecture.md](service-architecture.md) — the
synthesized high-level service architecture built from this document and
[major_calls.md](major_calls.md)/[assumptions.md](assumptions.md).

# Agent Orchestration Framework — Requirements (Working Draft)

Status: living document, captured incrementally during requirements brainstorm.
Legend: `[DECIDED]` locked in from discussion, `[OPEN]` still to resolve, `[FUTURE]` explicitly deferred but must not be architecturally foreclosed.

## 1. Context

Enterprise agent orchestration framework. Agents are given access to tools (bash,
web-search, code execution, ...) and run on behalf of external SaaS tenants.
Core pillars: sandboxing, scale, failure handling, rate limiting, tenant isolation.

## 2. Functional Requirements

| ID | Requirement | Status |
|----|-------------|--------|
| FR1 | Agents are defined with a configurable set of tools they may call (bash, web-search, code execution, extensible to more). | DECIDED |
| FR2 | Execution-tool calls (arbitrary code/CLI) run inside an isolated sandbox. The sandbox is created lazily, on a session's first execution-tool call, and reused for the rest of that session rather than re-provisioned per call. API-tool and agent-tool calls (FR15) don't need a sandbox at all. | DECIDED |
| FR3 | Tenants bring their own credentials/API keys for tools; these are stored per-tenant and never cross tenant boundaries. | DECIDED |
| FR4 | Tool calls vary from seconds (web-search, small bash/code) to hours (heavy code execution) to days (human-in-the-loop reply). The framework must support all three as first-class, not just the short case. | DECIDED |
| FR5 | An agent session must be resumable after a pod/process failure, picking up from its own record of what already happened rather than starting over or re-running completed calls. | DECIDED |
| FR6 | Rate limits are enforced per tenant (first increment). Design must not preclude adding per-agent and per-model limits later. | DECIDED (per-tenant), OPEN (per-agent/per-model — defer, don't foreclose) |
| FR7 | LLM access goes through a provider-agnostic interface. Single provider (Anthropic) wired up now; multi-provider must be addable without reworking the core. | DECIDED |
| FR8 | A waiting agent (e.g. mid human-in-the-loop, or queued) must consume ~0 compute — no process/pod held open per waiting agent. | DECIDED |
| FR9 | Runaway tool executions are hard-killed at a wall-clock limit, independent of agent/LLM logic. | DECIDED |
| FR10 | Full observability: tracing a session from model call through tool call to sandbox, plus per-tenant and per-session cost monitoring (LLM token spend, compute spend). | DECIDED |
| FR11 | Audit trail of agent/tool actions for compliance purposes, at command-level granularity for execution-tool calls — must be able to answer "show every command agent X ran on a given day." Tension with FR12 (secret embedded in a command string, e.g. `--token=...`) mitigated by design, not fully solved: credentials are injected outside the agent's view (a credential helper or proxy substitutes the value at execution time), so the agent never constructs a command string containing the raw secret, and it therefore shouldn't appear in audit text by construction. **Residual risk, unsolved, production item**: a command could still legitimately read and print a secret it has access to (e.g. dumping an env var, reading a file, an API response echoing a token) — general in-command output redaction is not solved. Tracked as **AUDIT-1** in [poc-vs-production.md](poc-vs-production.md). | DECIDED (exists, mitigated by design), OPEN (retention/compliance regime; residual output-redaction risk is a production item) |
| FR12 | No sensitive data (tenant credentials, secrets, raw PII) is ever logged or persisted outside the dedicated secret store — audit/observability data is redacted by construction. | DECIDED |
| FR13 | Tool credentials never enter the LLM's context: the model sees only a tool name + parameters and decides to call it on that basis; the actual credential value is injected at the network layer, after the model's decision, never before or visible to it. See [service-architecture.md](service-architecture.md) for which component does the injecting. | DECIDED |
| FR14 | Every tool call is attributable to the agent, the tenant, *and* the triggering human user (not just the tenant) — requires a `USER` concept in the data model, not just `TENANT`. | DECIDED |
| FR15 | Tool calls split into three categories with different handling: **API tools** (HTTP calls to internal/third-party services using tenant credentials), **execution tools** (arbitrary Python/Node/Bash and CLIs like pandoc/gh/jq/curl/git), and **agent tools** (an agent calling another agent — its own identity/tenant/audit trail carried along, through the same path any run starts through, no side channel around authorization). Only execution tools need a sandbox; API tools go through a lighter-weight credential-injecting proxy instead, and an agent tool call needs neither — it just starts a new session for the called agent and waits for its result. See [service-architecture.md](service-architecture.md). | DECIDED |
| FR16 | Every model call an agent's reasoning makes goes through the platform's LLM gateway — there is no path for a model call to happen off it. In the default design the reasoning loop is our own trusted platform code, so this is automatic: the loop simply has no other way to reach a model. It becomes a real constraint, not just a default, once a tenant's own reasoning code is allowed to run in place of ours (FR17) — that code must still be forced through the same gateway for rate limiting (FR6) and cost metering to hold. See [service-architecture.md](service-architecture.md). | DECIDED |
| FR17 | Only the code a sandbox is asked to run is sandboxed — the reasoning loop that decides what to call is trusted platform code and is not itself sandboxed. A sandbox's only network path is to the platform's tool-call proxy: no direct reach to the internet, the LLM provider, or any secret. This keeps a malicious or buggy tool call contained even though the loop issuing it is not. **[FUTURE]** If a tenant is ever allowed to bring its own reasoning loop in place of ours, that loop stops being trusted platform code and must run under this same rule — sandboxed, with its model calls forced through the LLM gateway (FR16) rather than made directly. See [service-architecture.md](service-architecture.md). | DECIDED |

## 3. Non-Functional Requirements

| ID | Requirement | Status |
|----|-------------|--------|
| NFR1 | Throughput: **~10k tool calls/*minute*** (~167/sec) at peak, confirmed as the actual target (not a stricter self-imposed bar) — corrected from an earlier draft that assumed 600x this. Architecture must scale ~10x further (~100k/min, ~1,670/sec) without a redesign — treat as a future scaling exercise, not a day-1 build target. At this scale, none of the engine/language leans made so far are under real throughput pressure. | DECIDED |
| NFR2 | Concurrency: ~1000 concurrent agents (spec says "100s distinct agents concurrently," with ~1000 used as the working number). Given FR8, "active" is dominated by suspended/durable state, not live processes — real pressure is on scheduler/queue throughput, not on holding live workers. | DECIDED |
| NFR3 | Tenant isolation is adversarial-grade: tenant-supplied prompts/code are assumed hostile. Sandbox boundary must hold against a malicious tenant, not just accidental misbehavior. | DECIDED |
| NFR4 | Security: encryption at rest and in transit for all tenant data and credentials; per-tenant secret isolation; least-privilege access to sandboxes and tool credentials. | DECIDED |
| NFR5 | Durability: no silent loss of agent progress on pod/node death (ties to FR5). | DECIDED |
| NFR6 | Extensibility: tools and LLM providers are pluggable via stable interfaces, not hardcoded integrations. | DECIDED |
| NFR7 | Portability: primary deployment is cloud-native K8s (new cluster, no existing one to integrate with); on-prem/hybrid is a follow-up — avoid gratuitous cloud-proprietary lock-in where it's cheap to do so, but don't over-engineer for on-prem now. | DECIDED |
| NFR8 | Cost efficiency: idle sandboxes and suspended agents must not accrue compute cost. | DECIDED |
| NFR9 | Compliance target (SOC2 / HIPAA / FedRAMP / none specified). | **Not required for POC** — deferred to production scope |
| NFR10 | Multi-region / data residency requirements. | **Not required for POC** — deferred to production scope |
| NFR11 | Availability target (SLA, e.g. 99.9%) and per-tenant SLA tiering. | **Not required for POC** — deferred to production scope |
| NFR12 | Architecture-significant decisions (e.g. durable execution engine choice) must be backed by an explicit cost + data-scale calculation (infra cost, storage growth under projected throughput/retention, throughput headroom) before being finalized. | DECIDED |
| NFR13 | Kubernetes-native by default: scheduling, isolation, scaling, and failure handling should use Kubernetes primitives. Any choice that isn't a native K8s primitive (e.g. a workflow engine, a sandbox runtime) must carry an explicit justification in its own topic document, not just an implicit assumption. | DECIDED |

## 4. Core Entities (conceptual)

Not a schema — just naming, in plain terms, the things the requirements
above assume exist. Full definitions of the services that own them live in
[service-architecture.md](service-architecture.md); this section only says
what each is for.

- **Agent** — a tenant-owned definition: version, model, system
  instructions, the tools it may call, and its environment config. (FR1,
  FR3, FR15)
- **Agent Session** — one run of an Agent, from start to
  done/cancelled/expired, pinned to the Agent version it started with.
  Carries a deadline, its current status, and running usage/cost totals.
  (FR4, FR5, FR8, FR9, FR10)
- **User** — the person, inside a tenant, who triggered a given Session.
  (FR14)
- **Secrets** — a tenant's credentials. Referenced by name wherever they're
  used or logged; the real value lives only in the secret store, never
  anywhere else. (FR3, FR12, FR13)
- Three further records back a Session without being part of the Session
  itself: the **session log** (the step-by-step record a Session resumes
  from), the **audit store** (the compliance record of every call — who,
  what, when, credential names only, never a value), and the **object
  store** (workspace files produced during a run). (FR5, FR8, FR11, FR12)

Tenant, Agent, and User are what a tool call needs to resolve to answer
"who did this, for whom" (FR14). The session log and audit store exist so
a Session can be resumed without a live process held open while it waits
(FR5, FR8), and so every call it made can be reconstructed afterward
(FR10, FR11).

See also: [poc-vs-production.md](poc-vs-production.md) — tracks every
deliberate POC-phase shortcut against a locked requirement, with a priority
for closing it before production. See also:
[major_calls.md](major_calls.md) — the register of big, hard-to-reverse
architectural decisions, for fast skimming.

## 5. Open Items (for next rounds)

- NFR9: Compliance target (SOC2 / HIPAA / FedRAMP / none)
- NFR10: Multi-region / data residency
- NFR11: Availability SLA and per-tenant tiering
- Durable-execution engine choice: see [durable-execution-engine.md](durable-execution-engine.md) — leaning Temporal (self-hosted) or custom-build, pending cost + data-scale calculation (NFR12)
- Credential storage mechanism (Vault, cloud KMS + envelope encryption, etc.)
- Sandbox design (tech choice, warm pools, resource limits, egress control): see [sandbox-design.md](sandbox-design.md)
- Durability & resilience (surviving pod restart/deployment/node drain mid-task): see [durability-and-resilience.md](durability-and-resilience.md)
- Observability & cost monitoring (live agent status, tracing, per-tenant cost): see [observability-and-cost-monitoring.md](observability-and-cost-monitoring.md)
- Implementation language & framework (Go vs. Python vs. polyglot): see [language-and-framework-choice.md](language-and-framework-choice.md)
- What is an agent — single tool-loop vs. graph-reasoning vs. multi-agent team, and whether an open-source framework's engine composes with Temporal: see [agent-model.md](agent-model.md) (foundational — may revise the entities above and the language choice above)
- State store (the actual DB behind the entities above — Postgres vs. CockroachDB vs. other, and how it relates to whatever the durable-execution engine needs): see [state-store.md](state-store.md) — a "must state" item per the spec
- How agents are defined and versioned (ties directly to the workflow-versioning concern already flagged in durable-execution-engine.md and durability-and-resilience.md): see [agent-definition-and-versioning.md](agent-definition-and-versioning.md) — a "must state" item per the spec
- Trust-level-dependent sandbox isolation (spec: agents have varying "trust levels") — added as a question in [sandbox-design.md](sandbox-design.md)
- Agent runtime / integration surface — what actually enforces the two-choke-point contract in code (sandbox egress policy + LLM proxy + MCP tool gateway, leaning away from a bespoke SDK): see [agent-runtime.md](agent-runtime.md) (MC-5)
