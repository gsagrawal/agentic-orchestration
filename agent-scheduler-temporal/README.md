# agent-scheduler-temporal

A proof of concept of the agent scheduler and runtime on Temporal, in Python: admit agent sessions,
run each as a durable Temporal workflow, and recover correctly when workers die. Background:
`../docs/service-architecture.md`. Each design choice and the alternative rejected: `DESIGN_CHOICES.md`.

**The property it exists to prove:** when a worker is killed in the middle of a tool call, the
session resumes on another worker, the old worker cannot change anything, and no tool effect
happens twice.

```
make test               # unit tests, Temporal test environment with time skipping (~20 s)
make test-integration   # dev server + real processes: kill -9, zombie, chaos, HTTP server (~3 min)
make demo               # one session run to the end on a worker process
make bench              # prints the measurements below (several minutes)
```

The Temporal servers (time-skipping test server and dev server) are **downloaded by the `temporalio`
SDK on first use** (`WorkflowEnvironment.start_time_skipping()` / `.start_local()`), so there is no
CLI or Docker step, but the host `temporal.download` must be reachable. Install:
`pip install -r requirements.txt`. Run the `make` targets from this folder.

## Architecture: who talks to whom

Temporal has three roles, and this PoC uses each for one component:

| Role | In this PoC | Does |
|------|-------------|------|
| **Client** | session manager (`session_manager.py`, via `temporal_engine.py`) | starts workflows; signals, queries and describes them. Runs no workflow code. |
| **Worker** | agent executor (`worker.py`) | hosts the code of the `AgentSession` workflow and the activities; runs it when Temporal hands it a task. |
| **Server** | the Temporal dev server | stores every session's history and hands tasks to workers. |

```
 client (HTTP API) ---> session manager ---gRPC---> Temporal server <---gRPC (long-poll)--- executor (worker)
                        quotas, who may talk       histories, timers,                       runs AgentSession
                        to which session           task queues                              and its activities
```

- **The manager and the executor never talk to each other.** The only channel is Temporal. The
  executor never calls the manager; the manager learns a session ended by asking (`describe`) when
  it checks a quota or serves a request. They share no database: the manager has `sessions.db`
  (tenant, user, active flag) and reads `agents.json`; the worker uses the fake tool, sandbox and
  notifier files under `--data-dir`.
- **A workflow is not a service or a process.** One `AgentSession` instance per session is a durable
  record of events on the server. When something happens to it (start, signal, timer, finished
  activity) the server hands a workflow task to some worker, which replays the history through the
  workflow code, decides the next step and returns. The next task may go to a different worker,
  which is why a killed worker does not lose the session.
- **Activities** (model, tool, sandbox, notification) also run in the executor process, but each is
  its own task that Temporal can retry on another worker.
- **Replaces the reconciler and epoch in `../docs/service-architecture.md`:** starting a workflow
  queues work for any live worker; Temporal's retries do the recovery; the activity attempt number
  does the fencing the epoch did.
- **Who runs Temporal:** nothing in this repo runs it as a service. The tests, demo and benchmark
  start a throwaway single-node in-memory dev server (`WorkflowEnvironment.start_local()`); `worker.py`
  and `server.py` take `--address` (default `localhost:7233`) and expect a server there. In
  production it would be a Temporal cluster or Temporal Cloud, run by whoever owns the platform.

### The engine interface (the manager does not depend on Temporal)

The manager reaches the thing that runs sessions only through `SessionEngine` (`engine.py`): `start`,
`status`, `open_step`, `reply`, `message`, `stop`. `temporal_engine.py` is the one implementation and
the only file on the manager's side that imports `temporalio`. To replace Temporal with an API call
or a message queue, write another class with those methods; `session_manager.py` and `api.py` do not
change. `tests/test_manager_without_temporal.py` runs the whole manager and API against an in-memory
engine to keep that true. Only the manager side is behind the interface: the workflow, activities and
worker are the Temporal-specific part by design, and would be rewritten for another engine.

## Layout (one module per component)

| Module | Component |
|--------|-----------|
| `api.py`, `session_manager.py` | session-manager: HTTP API (FastAPI), admission, per-tenant quotas, reply / message / stop. No Temporal imports. |
| `engine.py`, `temporal_engine.py` | the interface the manager uses to reach the runner, and its Temporal implementation |
| `workflow.py`, `activities.py`, `models.py` | agent-executor: the `AgentSession` workflow and its activities |
| `worker.py` | agent-executor: worker process |
| `server.py` | session-manager: runs the HTTP API (`python -m agent_scheduler_temporal.server`) |
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
- Worker processes (`python -m agent_scheduler_temporal.worker`), as separate OS processes. The crash tests use a
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
- **History is carried whole through continue-as-new.** That bounds the number of Temporal events,
  not the size of the payload; a long session with real model output would hit Temporal's payload
  size limit. A real design keeps the history in the session log (object store) and carries a
  pointer. Not measured here: the fake steps are a few hundred bytes.
- **A session stopped by the active-run limit gets no cleanup**, so its sandbox is not stopped by
  the workflow (the real sandbox controller has its own deadline; the fake would leak).
- **The active-run limit is a lifetime limit** (it includes waiting); see DESIGN_CHOICES 32.
- No real agent-crud-service, ingress/auth, sandbox, model, secret-manager or network proxy.
  No `reject` response, no cancel of a human wait other than stop, no per-tenant worker pools.
- No metrics, tracing or audit store. Usage and cost totals are not tracked.
- Each tool call holds a thread for its whole duration (Python's default pool, a few per core), which
  caps concurrent tool calls per worker. Not tuned or measured.
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
| Fencing: lower attempt rejected; a late zombie cannot record a result | `test_tool_service.py` |
| Fencing: a frozen worker, resumed after another finished, is rejected and causes no second effect | `test_crash.py::test_a_frozen_worker_...` |
| Crash recovery: `kill -9` at four points (before marker, before effect, after effect, after result), idempotent and non-idempotent; `kill -9` from outside mid-call | `test_crash.py` |
| "Outcome unknown" appears in the model's input and the call is not repeated; no marker means run | `test_crash.py`, `test_tool_service.py` |
| Manager and API without Temporal, against an in-memory engine | `test_manager_without_temporal.py` |
| Humans: reply for the open step, wrong step, duplicate, after the session ended or expired (410), no worker to answer (503), tenant and responder checks, message during a tool call, stop, agent version pinned for the session | `test_api.py` |
| Limits: tenant quota isolation; poison script and step cap; waiting expiry; waiting holds no worker; execution timeout; continue-as-new | `test_api.py`, `test_workflow.py`, `test_limits.py` |
| Invariant under chaos: 24 sessions, 4 workers, random SIGKILLs, no duplicate effect | `test_chaos.py` (+ `test_invariants.py` proves the checker can fail) |

## Measurements

Produced by `make bench` on the final code (re-run after the review fixes); copied here from that run.

Machine: 4 vCPU, Intel(R) Xeon(R) Processor @ 2.10GHz; Python 3.13.18; dev server in-memory, one node.

### Throughput (60 sessions x 3 tool calls, tool delay 0, 3 runs each)

| workers | sessions/s per run | median |
|---|---|---|
| 1 | 24.8, 24.8, 27.0 | 24.8 |
| 2 | 27.6, 28.2, 27.7 | 27.7 |
| 4 | 30.5, 26.9, 25.4 | 26.9 |

### Worker death to resume (20 trials, 2 workers, SIGKILL mid tool call)

Settings: heartbeat interval 1.0 s, heartbeat timeout 3.0 s, sticky queue timeout 2 s, activity retry backoff starts at 1 s, tool delay 2 s.

| measure | p50 | p95 | min | max |
|---|---|---|---|---|
| kill -> next attempt starts (s) | 3.99 | 4.00 | 3.98 | 4.00 |
| kill -> session finished (s) | 10.07 | 10.09 | 10.06 | 10.10 |

n = 20 per row; with this n the p95 is set by the two or three slowest trials.

What the numbers do and do not say:

- **Throughput does not grow with workers here** (about 25 sessions/s with one worker, about 27
  with two or four). With everything on 4 shared cores, the likely limit is the dev server and the
  client, not the workers; this was not investigated, so treat that as a hypothesis. These numbers
  say nothing about a real cluster.
- **Death to resume is almost exactly the settings.** The next attempt starts about 4.0 s after the
  kill: the 3 s heartbeat timeout plus the 1 s first retry backoff. The spread is tiny because both
  are fixed timers; the surviving worker was already polling. It would be longer if no worker were
  free, and with a frozen (not killed) worker the poller can be handed the retry too (see
  DESIGN_CHOICES 30). Finishing takes about 6 s longer than the restart: the retried tool call
  sleeps 2 s, and the sticky-queue timeout (2 s) may add to it. That split was not measured, and
  about 2 s of the 6 is unexplained.
- Lowering the heartbeat timeout makes recovery faster and false alarms more likely on a slow worker.
  Only the 3 s value was measured.

