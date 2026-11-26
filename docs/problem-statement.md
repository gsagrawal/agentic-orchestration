# Problem Statement (Source of Truth)

Verbatim, as given. Every other document in `docs/` should trace back to
this; where another document's assumptions diverge from this, that's a bug
to fix, not a reason to edit this file.

---

Design an orchestration framework that runs on the order of 100s distinct
agents concurrently on Kubernetes, where any agent may call tools, execute
code, and run shell commands and CLIs, without one agent being able to harm
another, the platform, or the tenant's data.

**Context to assume:**
- Agents are heterogeneous: different system prompts, tool sets, models,
  owners, and trust levels. Some run for seconds, some for hours, some wait
  days for a human reply.
- Tenants are enterprises. A tenant's agents must never see another
  tenant's data, credentials, or filesystem. Assume auditors will ask "show
  me every command agent X ran last Tuesday".
- Agents call two kinds of tools: API tools (HTTP calls to internal and
  third-party services with tenant credentials) and execution tools (run
  Python, Node or Bash; call CLIs such as pandoc, gh, jq, curl, git).
- Traffic is bursty. A tenant may spin 300 agents at 9am and none at noon.
- The LLM provider is external, rate-limited, and the single largest cost
  line.

**Hard requirements the design must address:**

| Requirement | What we mean |
|---|---|
| Scale | ~1000 concurrent agents, ~10k tool calls/min at peak, with a plan for 10x |
| Safe tool calls | An agent cannot call a tool it was not granted; credentials never enter the model context; every call is attributable to agent, tenant and triggering user |
| Kubernetes-native | Scheduling, isolation, scaling and failure handling use Kubernetes primitives or justify why not |
| Sandboxed execution | Agents run arbitrary code, shell commands and CLIs (pandoc, gh, and others) in an environment where escape, lateral movement and data exfiltration are contained |
| Durability | An agent mid-task survives a pod restart, a node drain and a deploy |
| Observability | An operator can answer "what is agent X doing right now and what has it cost so far" |

