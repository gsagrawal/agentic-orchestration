# Sandbox Design — Open Discussion

Status: **OPEN** — not yet discussed in depth.

Referenced from: [requirements.md](requirements.md) §5 (Open Items).

## Why this decision matters

Ties to NFR3 (tenant code is assumed adversarial, not just misbehaving), FR2
(every tool call runs in an isolated sandbox), FR4 (sandbox lifetimes range
from seconds to hours), and FR9 (hard wall-clock kill for runaway execution).
This is the data-plane counterpart to the durable-execution engine — the
engine decides *when* a tool call happens, the sandbox decides *where and how
safely* it runs.

## Decided requirement (not yet a mechanism)

Per [assumptions.md](assumptions.md) and FR17: the agent process itself must
run with **no network egress other than the two mandatory choke points**
(the tool-call gateway and the LLM-call gateway). This is a settled
requirement on *what* the sandbox must guarantee — it's what makes treating
agent internals as opaque for durability/bookkeeping safe rather than a
blind spot. *Which* sandbox technology enforces this is still fully open
below. See [agent-runtime.md](agent-runtime.md) (MC-5) for how the two
choke points themselves are realized (egress policy + proxy + MCP gateway)
— this document is about the sandbox boundary around that, not the gateway
mechanics.

## Questions to resolve

- **API tools vs. execution tools** (per
  [problem-statement.md](problem-statement.md)): the spec splits tool calls
  into API tools (HTTP + tenant credentials) and execution tools (arbitrary
  Python/Node/Bash, CLIs like pandoc/gh/jq/curl/git). Only execution tools
  obviously need a full sandbox; API tools plausibly go through a
  lighter-weight credential-injecting proxy instead of a microVM per call —
  cheaper and faster for the likely-high-volume API-tool path. Not yet
  decided whether API tools get any sandbox at all, or just the proxy.
- **Trust-level-dependent isolation — RESOLVED**: uniform, strongest-sandbox
  baseline regardless of an agent's stated trust level. Trust level does not
  weaken isolation for any agent; it may still gate *other* things (tool
  grants, rate limits) but not sandbox strength.
- **Kubernetes-native-or-justify** (NFR13): the spec requires justifying any
  choice that isn't a native K8s primitive. A microVM sandbox runtime
  (Firecracker/Kata) is not itself a K8s primitive — whatever is chosen here
  needs an explicit justification paragraph for why K8s's own isolation
  primitives (e.g. plain containers/namespaces) aren't sufficient against
  the adversarial bar in NFR3.
- **Isolation technology**: gVisor vs. Firecracker/Kata (microVM) vs.
  container+seccomp. No tech has been chosen yet — nothing below is decided.
- **Warm pools vs. cold provisioning**: the "<5s" tool-call class (web search,
  small bash/code) likely wants pre-warmed sandboxes to avoid cold-start
  latency; the "hours" class can tolerate cold provisioning. Need a strategy
  that serves both without over-provisioning idle warm capacity.
- **Resource limits enforcement**: CPU/mem/disk/network caps and the
  wall-clock kill (FR9) — enforced by the sandbox runtime itself, not by
  agent-side cooperation.
- **Network egress control**: per-tenant allowlisting so a sandboxed tool
  call can't reach arbitrary destinations.
- **Credential injection**: how a tenant's own tool credentials (FR3) get into
  a sandbox at execution time without ever landing in an image, a log, or a
  checkpoint (ties to FR12).
- **Multi-tenancy placement**: dedicated node pools per tenant vs. shared
  nodes with strong per-sandbox isolation — cost vs. isolation-strength
  trade-off.
- **Scaling the sandbox pool**: supporting the 10k+/sec peak (NFR1) with
  room for the 10x future headroom, without that pool itself becoming the
  bottleneck.

## Decision log

- 2026-11-25: Trust-level-dependent isolation resolved — uniform,
  strongest-sandbox baseline for every agent regardless of stated trust
  level. Isolation technology itself (gVisor/Firecracker/Kata/etc.) remains
  open.
