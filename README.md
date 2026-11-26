# Agent scheduler and runtime (MVP)

Admits agent sessions, assigns each to exactly one executor, keeps it alive, and recovers
it when an executor dies. Design background: `docs/service-architecture.md`. Every design
choice, with the alternative rejected: `DESIGN_CHOICES.md`.

**The property this code exists to prove:** when an executor is killed in the middle of a
tool call, the session resumes on another executor, the old executor can no longer change
anything, and no tool effect happens twice.

```
make test    # about 30 s; includes real kill -9 and a chaos test
make demo    # 12 sessions, one executor killed mid-run, outcome and invariants printed
make bench   # throughput and death-to-resume timing; takes several minutes
```

## How the property is enforced

| Mechanism | Where |
|-----------|-------|
| **Epoch fencing.** Every assignment bumps the session's epoch. Every log append, tool call and release carries it; a stale epoch is rejected inside the same transaction that would have made the change. | `session_log.py`, `tool_service.py`, `state_table.py` |
| **Idempotency keys.** A tool call's key is `<session>:<step>`; the tool service stores the result per key, so a retry returns the stored result instead of running again. | `tool_service.py` |
| **"Outcome unknown".** On resume, a call that is logged but has no result is looked up by key. Found: use it. Not found and the tool is idempotent: run it again. Not found and it is not: tell the model "outcome unknown"; never retry. | `executor.py` (`_recover_call_in_flight`) |
| **Reconciler.** One loop ends sessions past a deadline, then gives every other live session a live executor. It does not need to know why an executor is gone. | `session_manager.py` |

## Layout (one file per component)

| File | Component |
|------|-----------|
| `orchestrator/db.py` | SQLite connection, schema, transactions |
| `orchestrator/state_table.py` | session-manager: the session state table, executor liveness queries |
| `orchestrator/session_manager.py` | agent-session-manager: admission, quotas, timers, reconciler |
| `orchestrator/session_log.py` | session log with epoch-checked, append-only writes |
| `orchestrator/executor.py` | agent-executor: replay, loop, crash recovery, worker process |
| `orchestrator/tool_service.py` | tool service stub: idempotency keys, epoch check, effects ledger |
| `orchestrator/invariants.py` | end-of-run checks used by the chaos test and the demo |
| `orchestrator/cluster.py` | local harness: manager loop in a thread, executors as processes |
| `orchestrator/fault.py` | fault injection (SIGKILL at a named point), tests only |
| `orchestrator/scripted_model.py`, `agent_store.py`, `fake_tools.py` | the faked parts, below |
| `scripts/` | `agents.json`, `demo.py`, `bench.py` |

## What is real

- The state table, admission (per-tenant quota, read-only budget flag, agent version pinning,
  run deadline, step cap), assignment with epoch bump, heartbeat and lease, reconciler loop.
- Executors as separate OS processes. `kill -9` in the tests is a real SIGKILL; zombies in
  the chaos test are real processes frozen with SIGSTOP and resumed with SIGCONT.
- The session log with epoch-checked appends and the replay that rebuilds an executor's
  state from it.
- The tool service's logic: idempotency-key store, epoch check, effects ledger.
- The two timers (active-run limit, waiting expiry) and the step cap.

## What is faked

| Fake | Stands in for | Note |
|------|---------------|------|
| `scripted_model.py`: a script of actions per agent, including a looping "poison" script | llm-gateway and the model | A pure function of the messages it is shown, like a real model |
| `fake_tools.py`: `lookup` and `set_value` (idempotent), `send_email` (not), with a configurable delay | external APIs | "Idempotent" means the backend dedupes on the key |
| `tool_service.py` as a whole | agent-network-proxy | **Stronger than a real proxy**: it re-checks the epoch in the same transaction that commits the effect. A real proxy cannot do that for an external API; a request already forwarded when the epoch changes still takes effect, and only the idempotency key protects its retry |
| `agent_store.py`: a JSON file | agent-crud-service | |
| `tenant_budget` table, written by tests | the rollup job | The manager only reads it, as designed |
| Not present at all | ingress, auth, secret-manager, sandbox-controller, audit store | |

## What is not built (gaps)

- **Answering a waiting session.** Nothing moves a `waiting` session back to `running`, so a
  waiting session can only expire. If added: the executor must read the human response
  before releasing, and the run deadline must restart on resume.
- **Reject / cancel**, and agent-as-tool child sessions.
- **Leader election.** Exactly one manager process may run the reconciler.
- **Unexpected exceptions kill the worker.** A bad agent id or unknown tool kills
  the whole worker and every session on it. Not decided; swallowing it would hide the problem.
- **Heartbeat during a long iteration.** One iteration (a slow tool call) longer than the
  lease gets a healthy executor fenced. Safe, not free.
- Executors take turns across their sessions in one thread, so a slow tool call delays the
  executor's other sessions.
- The rollup job, usage metering, per-executor capacity limits, retention of old rows.

## The SQLite caveat

SQLite on one machine proves the **logic** (fencing, replay, idempotency, recovery), not
distributed behaviour. Specifically:

- One machine means one clock. Here the manager and executors compare `time.time()` values
  directly. With separate machines, **lease checks under clock skew** are a real problem: a
  skewed executor looks dead, or a dead one looks alive.
- There is one write lock. `BEGIN IMMEDIATE` gives us exact serialisation for free. On
  Postgres this becomes **row locks** (`SELECT ... FOR UPDATE`, or a conditional `UPDATE`
  as used here, which still works), with deadlocks, lock timeouts and isolation levels to
  think about.
- There are no **network partitions**. A real executor can be cut off from the database
  while still running and still talking to the tool service. Fencing at the log is not
  enough then: the tool service must check the epoch against the same source of truth, and
  the window between "check" and "forward the request" cannot be closed by us.
- Dispatch here is polling the table. A real queue brings its own delivery semantics
  (visibility timeouts, redelivery) on top of ours.
- `synchronous=NORMAL` survives `kill -9` of a process, not necessarily an OS crash or power
  loss.
- The chaos test and the benchmark share four CPU cores with their own database; the
  numbers below say nothing about a database server.

## Measurements

Machine: Intel Xeon @ 2.10 GHz, 4 logical CPUs, Linux 6.18, Python 3.13.18, SQLite 3.53.1 (a
cloud container, other processes may share the host). Produced by `make bench`, one run, on
the final code; the manager's reconcile pass is every 0.05 s and the executor polls every 0.02 s.

**Throughput.** 200 `quick` sessions (two tool calls each), seconds from the first admission
until all have ended, 3 runs per cell, median shown (all runs in parentheses).

| Executors | Tool delay 0.01 s (sessions/s) | Tool delay 0 (sessions/s) |
|-----------|-------------------------------|---------------------------|
| 1 | 31.4 (31.5, 31.4, 31.4) | 957 (872, 1056, 957) |
| 2 | 62.0 (62.1, 62.0, 61.7) | 1027 (1064, 1027, 954) |
| 4 | 122.5 (122.5, 123.2, 121.3) | 963 (1195, 921, 963) |
| 8 | 234.0 (234.0, 227.4, 239.2) | 807 (878, 807, 806) |

How to read it, and what I did not find out:
- With a 10 ms tool delay the executors are mostly asleep, so throughput is set by the delay
  and grows in proportion to the executor count, even at 8 executors on 4 cores. That shows
  sessions spread across executors without interference. It is **not** a capacity figure.
- With no delay, throughput stays around 800 to 1,200 sessions/s whatever the executor count,
  and 8 executors are slower than 1. Something shared is the limit. I did not isolate it;
  candidates are the single SQLite write lock, the manager's 0.05 s pass, and admitting
  sessions one by one from a single thread. Each of these runs lasts about 0.2 s, so the
  numbers are noisy (range 806 to 1,195).

**Executor death to resumed session.** 4 executors, 40 never-ending sessions, tool delay
0.02 s, one randomly chosen executor SIGKILLed per trial, 8 trials. "Resumed" means the first
log entry written for the session by a new epoch, polled about every 5 ms. n = 80 sessions
(the 10 or so sessions displaced by one kill share one detection moment, so they are not
independent samples; the independent count is 8 kills).

| Lease / heartbeat | p50 | p95 | max |
|-------------------|-----|-----|-----|
| 0.6 s / 0.1 s | 0.69 s | 0.80 s | 0.82 s |
| 3.0 s / 0.5 s | 2.83 s | 3.06 s | 3.10 s |

The time is essentially the lease: the manager cannot act before the lease runs out. The p50
is a little under the lease because the last heartbeat was already partly aged when the
executor died; the p95 is a little over, which is the reconcile pass, the replay and the first
write. I did not measure those parts separately. The default lease (3 s) and heartbeat
(0.5 s) are assumptions, not tuned values.

## Tests, by property

| Property | Tests |
|----------|-------|
| Fencing | `test_session_log.py` (stale, future, zombie, race against a reassignment), `test_tool_service.py` (stale epoch, zombie fenced at the effect), `test_executor.py` (zombie rejected on its next write), `test_limits.py` (an overdue run is fenced) |
| Crash recovery | `test_crash_recovery.py`: real `kill -9` at all nine points of a call for an idempotent and a non-idempotent tool, plus kills from outside mid-delay; in-process tests of each recovery rule and of what the model is shown |
| Limits | `test_session_manager.py` (quota, other tenants unaffected, race for the last slot), `test_limits.py` (step cap stops the poison script; waiting expiry uses no executor; the two timers are independent) |
| Invariant | `test_chaos.py`; `test_invariants.py` checks that the checker itself catches each violation |

### How sharp is the chaos test? (measured)

A chaos test that never fails proves nothing, so each mutation below was applied to the code
and `test_chaos.py` run (3 runs of 2 seeds each, so 6 per row). The narrow windows are covered deterministically
elsewhere; chaos is the net under them, not the proof of them.

| Mutation | Result |
|----------|--------|
| Log append without the epoch fence | fails 6 of 6 runs |
| Tool service without the in-transaction epoch re-check | fails 3 of 6 runs (the zombie must be frozen exactly before its effect) |
| Always retry a call in flight, even for a non-idempotent tool | fails 6 of 6 runs |

The schedule is seeded; process timing is not, so a failing seed is a lead, not a replay.


---

# Temporal runtime (`agent_runtime/`)

The scheduler and runtime on Temporal, in Python. It sits beside the older `orchestrator/` above
(a different design: state table + reconciler) and does not touch it. Each design choice and the
alternative rejected: `agent_runtime/DESIGN_CHOICES.md`.

**The property it exists to prove:** when a worker is killed in the middle of a tool call, the
session resumes on another worker, the old worker cannot change anything, and no tool effect
happens twice.

```
make test-runtime       # unit tests, Temporal test environment with time skipping (~20 s)
make test-integration   # dev server + real worker processes: kill -9, zombie, chaos (~3 min)
make demo-runtime       # one session run to the end on a worker process
make bench-runtime      # prints the measurements below (several minutes)
```

The Temporal servers (time-skipping test server and dev server) are **downloaded by the `temporalio`
SDK on first use** (`WorkflowEnvironment.start_time_skipping()` / `.start_local()`), so there is no
CLI or Docker step, but the host `temporal.download` must be reachable. Install:
`pip install -r requirements.txt`. (`make demo` and `make test` still run the older code.)

## Layout (one module per component)

| Module | Component |
|--------|-----------|
| `api.py`, `session_manager.py` | session-manager: HTTP API (FastAPI), admission, per-tenant quotas, reply / message / stop |
| `workflow.py`, `activities.py`, `models.py` | agent-executor: the `AgentSession` workflow and its activities |
| `worker.py` | agent-executor: worker process |
| `tool_service.py`, `tools.py` | tool service: results by key, started markers, attempt fencing, effects ledger; tool metadata |
| `fault.py`, `harness.py`, `chaos.py`, `invariants.py` | test support: fault injection, worker processes, chaos run, end-of-run checks |

## How the property is enforced

| Mechanism | Where |
|-----------|-------|
| One workflow per session, id = session id. It only decides; every outside call is an activity. Replay-tested. | `workflow.py` |
| **Idempotency key** = session id + step. The tool service stores the result per key; a retry gets it back. | `tool_service.py` |
| **Fencing.** Temporal's activity attempt number goes with every call; the tool service keeps the highest per key and rejects lower ones, at the start and when recording a result. | `tool_service.py` |
| **Outcome unknown.** A "started" marker is committed before the tool runs. Retry finds marker and no result: idempotent tool runs again; non-idempotent tool returns "outcome unknown" to the model and is not called. No marker: safe to run. | `tool_service.py` |
| **Heartbeats** so a dead worker is noticed in seconds, not at the tool's timeout. | `activities.py`, `models.py` |
| **Waiting costs no worker:** a timer plus a signal; the session holds nothing while it waits. | `workflow.py` |

## What is real

- The `AgentSession` workflow and activities (`call_model`, `ensure_sandbox`, `run_tool`, `stop_sandbox`,
  `request_human`, `mark_expired`); signals for reply, message and stop; step cap; waiting expiry;
  active-run limit; continue-as-new.
- Worker processes (`python -m agent_runtime.worker`), as separate OS processes. The crash tests use a
  real `SIGKILL`; the zombie test uses a real `SIGSTOP` / `SIGCONT`.
- The session manager API and per-tenant admission quota.
- The tool service's logic, on a real SQLite file shared by the processes.
- The tests below, including the chaos run.

## What is faked

| Fake | Stands in for |
|------|---------------|
| `scripted_model.py`: a script per agent, including a looping "poison" script | llm-gateway and the model |
| `tools.py`: `lookup`, `set_value` (idempotent), `send_email`, `run_code` (not), with a delay | external APIs and the network proxy |
| `tool_service.py` as a whole | agent-network-proxy and the external APIs behind it |
| `sandbox.py`: records calls, says whether a sandbox was newly created | sandbox-controller |
| `notifier.py`: keyed notifications, expiry marks | notification service and the manager's expiry callback |
| `agents.json` + `agent_store.py` | agent-crud-service |
| Trusting the `X-Tenant` / `X-User` headers | ingress and auth |
| `fault.py` (env-var driven self-kill) | nothing real; test support only |

## What is not built (gaps)

- **Fencing does not stop a zombie that is frozen *after* it passed the check and *before* its
  effect.** The tool service rejects stale attempts when they call it and when they record a result,
  but an effect already on its way (a real external API cannot be fenced) still lands, late. For the
  same key that is not a duplicate: an idempotent backend dedupes by key, and a non-idempotent retry
  reports "outcome unknown" instead of running. The hazard is that the effect can land after the
  session has moved on, and the model may act on "unknown" (for instance by trying again under a new
  step, a new key). Not tested: the chaos run uses SIGKILL, which cannot act after dying.
- **A session stopped by the active-run limit gets no cleanup**, so its sandbox is not stopped by
  the workflow (the real sandbox controller has its own deadline; the fake would leak).
- **The active-run limit is a lifetime limit** (it includes waiting); see DESIGN_CHOICES 32.
- No real agent-crud-service, ingress/auth, sandbox, model, secret-manager or network proxy.
  No `reject` response, no cancel of a human wait other than stop, no per-tenant worker pools.
- No metrics, tracing or audit store. Usage and cost totals are not tracked.
- The reply/message/stop API takes a session id the caller already knows; there is no status or list endpoint.
- The manager's quota table is one SQLite file; it is not safe to run more than one manager against
  a real shared database without redesign.
- Retention, history size limits and workflow versioning (changing workflow code under running
  sessions) are not addressed.

## Caveat: SQLite and a single-node dev server

SQLite and a single-node dev server with in-memory persistence prove the *logic*, not production
behaviour. With a real Temporal cluster you would see: durable multi-node history, real network
latency and partitions between workers and the server (so zombies are common, not rare), many
workers per task queue and rebalancing, and different timeout and retry behaviour under load. With
a real database for the tool service: network latency in every call, a different locking model (SQLite
has one writer; `BEGIN IMMEDIATE` is how that is used here), the need for real transactions across
the effect and the ledger, and failover. The measurements below are from one 4-core machine where the
server, the workers and the client all compete; they are not capacity numbers.

## Tests, by property

| Property | Tests |
|----------|-------|
| Fencing: lower attempt rejected; a late zombie cannot record a result | `test_agent_runtime_tool_service.py` |
| Fencing: a frozen worker, resumed after another finished, is rejected and causes no second effect | `test_agent_runtime_crash.py::test_a_frozen_worker_...` |
| Crash recovery: `kill -9` at four points (before marker, before effect, after effect, after result), idempotent and non-idempotent; `kill -9` from outside mid-call | `test_agent_runtime_crash.py` |
| "Outcome unknown" appears in the model's input and the call is not repeated; no marker means run | `test_agent_runtime_crash.py`, `test_agent_runtime_tool_service.py` |
| Humans: reply for the open step, wrong step, duplicate, after expiry (410), tenant and responder checks, message during a tool call, stop | `test_agent_runtime_api.py` |
| Limits: tenant quota isolation; poison script and step cap; waiting expiry; waiting holds no worker; execution timeout; continue-as-new | `test_agent_runtime_api.py`, `test_agent_runtime_workflow.py`, `test_agent_runtime_limits.py` |
| Invariant under chaos: 24 sessions, 4 workers, random SIGKILLs, no duplicate effect | `test_agent_runtime_chaos.py` (+ `test_agent_runtime_invariants.py` proves the checker can fail) |

## Measurements

Produced by `make bench-runtime`; copied here unedited from the run that produced them.

<<BENCH>>
