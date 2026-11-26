# Durability & Resilience — Open Discussion

Status: **OPEN** — not yet discussed in depth.

Referenced from: [requirements.md](requirements.md) §5 (Open Items). Closely
related to [durable-execution-engine.md](durable-execution-engine.md), but
scoped differently: that document picks the engine that models an agent run
as resumable state; this one covers the operational mechanics of actually
surviving a pod restart, a rolling deployment, or a node drain mid-task.

## Why this decision matters

Ties to FR5 (resume from last checkpoint after a pod/process failure) and
NFR5 (no silent loss of agent progress on pod/node death). The engine choice
gives us the primitive (durable, checkpointed state); this discussion is
about what has to happen operationally around that primitive for it to
actually hold under real failure modes.

## Questions to resolve

- **Graceful shutdown**: on SIGTERM (rolling deployment, scale-down), how
  does a worker checkpoint or hand back an in-flight step before the pod
  dies, instead of just losing it?
- **Node drain / eviction**: PodDisruptionBudgets and how the system detects
  a worker is gone (vs. just slow) and reassigns its work — lease/heartbeat
  timeouts and the trade-off between fast failover and false-positive
  reassignment.
- **Deployment rollout vs. in-flight runs**: when new agent/workflow code
  deploys while runs are mid-flight (possibly mid human-in-the-loop wait that
  started days earlier under the old code version), how do in-flight runs
  stay correct? This is the same concern as item 6 (versioning) in
  durable-execution-engine.md, but from the deployment-process side rather
  than the engine-API side.
- **Different failure handling per tool-call class**:
  - Short in-flight tool call at crash time → safe to just retry
  - Long-running sandboxed execution in-flight → does the sandbox keep
    running independently of the orchestrator pod that dispatched it, or
    does it die with the pod and get retried from scratch? (Cost and
    correctness implications for hour-long code execution.)
  - Human-in-the-loop wait → should be entirely unaffected by pod death,
    since the wait state lives in persisted storage, not in any process
  - Each class likely needs a different answer here; not yet worked out.
- **Idempotency on resume**: ensuring a reassigned/retried step can't
  double-execute a side-effecting tool call (e.g. a bash command with side
  effects) — same concern flagged as item 5 in durable-execution-engine.md.

## Decision log

(none yet)
