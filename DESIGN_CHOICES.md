# Design choices

Choices made while building, each with the alternative rejected. Numbers are stable and
sections are by topic, so a few early entries are marked superseded rather than deleted.

The seven decisions given in the brief (epoch fencing, at-least-once tools with idempotency
keys, "outcome unknown", two timers, step cap and quotas, read-only budget status, swappable
state plumbing) are implemented as specified and are not repeated here.

## State and log

| # | Choice | Rejected alternative | Why |
|---|--------|----------------------|-----|
| 1 | The fence and the sequence number are one statement: `UPDATE sessions SET log_seq = log_seq + 1 WHERE id=? AND epoch=? RETURNING log_seq`, followed by the INSERT, inside one `BEGIN IMMEDIATE` transaction. | Check the epoch with a SELECT, then INSERT with `MAX(seq)+1`. | A separate check can be interleaved with a reassignment. One conditional UPDATE cannot, and it also gives a gap-free seq without a second query. |
| 2 | `log_seq` is a column on `sessions`. | Compute `MAX(seq)` from the log. | The UPDATE needs a column to touch so it can double as the fence. Cost: one more column on the state row. |
| 3 | Each log entry stores the epoch of its writer. | Store only the seq. | Lets the chaos test audit "no log entry carries a stale epoch" from the data itself. |
| 4 | Append-only is enforced by SQLite triggers that abort UPDATE and DELETE on the log. | Rely on code discipline. | Cheap, and a test proves it. Does not stop someone dropping the trigger; it guards against our own bugs, not a malicious DB user. |
| 5 | `assign()` always bumps the epoch, even if the session already has that executor. | Skip the bump if the executor is unchanged. | A restarted process with the same id must still fence its previous incarnation. |
| 6 | Reads from the log need no epoch. | Fence reads too. | Replay is read-only and harmless; only writes and tool calls change anything. |
| 7 | Plain `StaleEpoch` and `UnknownSession` are different exceptions. | One error for both. | A stale epoch is a normal fencing event; an unknown session is a bug and should not look routine. |
| 8 | States: `created, running, waiting, done, cancelled, expired, stopped`, enforced by a CHECK constraint. `stopped` means cut off by the step cap or active-run limit. | The architecture doc's `admitted`/`resumed`. | `admitted` and `created` are the same moment in this MVP, and `resumed` is just `running` with a higher epoch. Fewer states, fewer transitions to test. |
| 9 | Separate `run_deadline` and `wait_deadline` columns. | One `deadline`. | Decision 4 requires two different timers; one column would conflate them. |
| 10 | One SQLite file for state table and log, one connection per process, WAL, `busy_timeout` 10 s, autocommit with explicit `BEGIN IMMEDIATE`. | Python's implicit transactions; separate files for log and state. | Implicit transactions start deferred and make read-then-write racy. One file keeps the epoch check and the insert atomic (two files could not). |
| 11 | `assign()` refuses terminal sessions (`SessionClosed`). | Let the caller check the state first. | A check outside the transaction races. |
| 12 | `synchronous=NORMAL`. | `FULL`. | Safe against `kill -9` (what the tests do) but may lose the last commits on OS crash or power loss. Chosen for speed; a real deployment would not accept this. |
| 70 | Secondary indexes on `sessions`: `(executor_id, state)`, `(state)`, `(tenant, state)`. | No indexes (the first version). | Found in review: executors poll `assigned_to` every 20 ms, admission runs `count_live`, and the reconciler lists candidates, and all three scanned every session ever created. Measured once with a throwaway script (not kept in the repo) on this machine, at 100,000 finished sessions: `assigned_to` 7.9 ms per call without indexes, 0.02 ms with them. A test checks the query plans so a scan cannot come back unnoticed. |

## Tool service stub

| # | Choice | Rejected alternative | Why |
|---|--------|----------------------|-----|
| 13 | The epoch is checked twice: once up front (fast reject), once inside the transaction that commits the effect. | Check once at the start. | A zombie can pass the first check and then sleep (a tool delay) while the session is reassigned. Only the in-transaction check stops it at the moment of the effect. Tested by changing the epoch from a fault hook. |
| 14 | The stub is stronger than a real proxy: effect, result and epoch check commit atomically. Labelled as faked in `tool_service.py`. | Pretend the real proxy can do this. | It cannot for an external API. Only the idempotency key protects a request that was already forwarded. |
| 15 | "Idempotent" means the backend dedupes on the key: effect and stored result commit together. A non-idempotent tool commits the effect first and records the result in a second step, so there is a window (effect, no result). | Same atomic commit for every tool. | Without the window, "non-idempotent tool with no recorded result" (decision 3) could never occur for real, and step 6 could not be tested honestly. |
| 16 | The result of a non-idempotent call is stored even if the epoch changed during the window. | Reject it as stale. | The effect already happened; recording it is the truth. The zombie's log append is still fenced. |
| 17 | No "started" intent record. `call()` on a non-idempotent tool with no stored result runs it again; the executor must decide not to call (see #48). | Service refuses to rerun a non-idempotent key that has no result. | Keeps to the brief: the "outcome unknown" rule lives in the executor. Cost: the service alone is not safe for non-idempotent retries (documented in the module docstring). |
| 18 | A key reused with a different tool or args raises `KeyConflict`. | Silently return the stored result. | The step number is replayed deterministically from the log, so a mismatch means a bug; failing loudly beats returning a wrong result. |
| 19 | The effects ledger has no unique constraint and is append-only (triggers). | Unique on (session, step). | A unique constraint would reject the second effect instead of recording it, hiding exactly what the tests must detect. |
| 20 | `fault_hook(point)` with points `before_effect` / `after_effect`. | Sleep-and-hope timing in tests. | Deterministic: tests (and later real `kill -9`) land at exactly the window being tested. |

## Executor

| # | Choice | Rejected alternative | Why |
|---|--------|----------------------|-----|
| 21 | Dispatch is the state table: `assign()` writes `executor_id`, the executor polls `assigned_to()`. | A queue or socket per executor. | One mechanism (the table) for dispatch, liveness and recovery; nothing extra to lose on a crash. Cost: polling (20 ms default). |
| 22 | The scripted model is a pure function of the messages; its script position is the number of assistant messages in the history. | Keep a script cursor in executor memory or in the state table. | The executor stays stateless: replay alone restores the position, as it would for a real LLM. |
| 23 | The log has two entry kinds for the loop: `model` (the decision, written BEFORE the tool runs) and `tool_result`. A `model` tool entry with no result after it is "a call in flight". | Log one entry after the tool returns. | Writing the decision first is what lets a resumed executor see that a call may have started. Step 6 depends on it. |
| 24 | One `Replay.apply()` serves replayed and live entries. | Separate replay and live code paths. | A resumed session cannot drift from an uninterrupted one. |
| 25 | A worker takes turns across its sessions, one loop iteration each, in a single thread. | A thread or process per session. | No threads, no shared connection problems. Cost: a slow tool delays the worker's other sessions (known, see README gaps). |
| 26 | Step number = count of tool calls in the log (1-based). Idempotency key = `<session>:<step>`. | A separate counter. | Derived from the log, so it cannot disagree with it. |
| 27 | `finish`/`wait` are logged, then the session is released in the same iteration; if the executor dies between, the next one sees the last action and releases. | Release first. | Log first means the log never lags the state table. |
| 28 | `release()` is fenced by epoch and by `state='running'`; the executor catches `StaleEpoch`, forgets the session and carries on. | Crash the worker on a fencing error. | Being fenced is normal after reassignment, not a failure. |
| 29 | **Superseded by #48.** Resuming with a call in flight first failed closed (`NotImplementedError`) rather than guess. | Retry the call (unsafe for non-idempotent tools). | It was a placeholder until the recovery rules existed. |
| 30 | `agent_id`/`agent_version` columns pin the definition at admission; the agent store is a JSON file. | Look up "latest" on each pickup. | Pinning is required by the architecture: edits must not change running sessions. |
| 31 | Workers are `multiprocessing` "spawn" processes. | "fork". | A forked child would inherit the parent's open SQLite connections. |

## Session manager: admission and assignment

| # | Choice | Rejected alternative | Why |
|---|--------|----------------------|-----|
| 32 | Callers see only a `SessionEngine` Protocol (`start_session`, `get_status`); the table + reconciler class implements it. | A concrete class used directly. | Decision 7: swapping in a workflow engine must not touch callers. Kept to two methods now; it grows as features land. |
| 33 | Quota counts every non-terminal session (`created`, `running`, `waiting`). | Count only `running`. | A waiting session still holds a row and will want an executor again; otherwise a tenant could pile up unlimited waiting sessions. Cost: a tenant with many sessions waiting for humans can block itself. |
| 34 | Quota check, budget check and insert are one `BEGIN IMMEDIATE` transaction. | Check, then insert. | Two concurrent admissions could both take the last slot. A thread race test fails without the transaction. |
| 35 | Budget is a flag in `tenant_budget` that admission only reads; the rollup job that would write it is not built. | Sum usage at admission. | Decision 6. Tests set the flag directly. |
| 36 | `run_deadline` is set at admission (now + limit). Defaults (`step_cap=50`, limit 300 s, 10 sessions per tenant) are assumptions, not measured. | Derive from data. | There is no data yet; labelled as assumptions in code. |
| 37 | Executors register themselves in an `executors` table; the manager assigns to the least-loaded one (fewest running sessions, ties by id). | Round robin; static executor list. | Least-loaded needs no extra state and spreads evenly after a failure moves sessions. No capacity limit per executor yet. |
| 38 | With no executor registered, sessions stay `created` and the reconciler retries. | Reject admission. | Admission is about the tenant's limits; capacity is the scheduler's problem. |
| 39 | The pinned version is the agent's highest version at admission. | Caller chooses the version. | Matches the architecture ("pins the Session to the Agent's current version"). |
| 40 | `reconcile_once()` is the single place that assigns, for first assignment and for dead executors alike (#44). Assumes one manager process (no leader election). | Assign inside `start_session`. | One code path for first assignment and for recovery, so it is tested once. |

## Liveness and the reconciler

| # | Choice | Rejected alternative | Why |
|---|--------|----------------------|-----|
| 41 | Liveness is derived: an executor is alive if its last heartbeat is at most one lease old, judged by the manager at reconcile time. Nothing stores "dead". | A status column the manager flips. | No second piece of state that can disagree with the heartbeat; a zombie that heartbeats again is simply alive again. |
| 42 | The heartbeat is sent from the executor's loop, between iterations. | A background heartbeat thread. | A thread would keep reporting "alive" while the loop is hung. The heartbeat is also sent between sessions, so the longest silence is one iteration, not one sweep over all its sessions (a bug the demo exposed: with 4 sessions per executor, healthy executors were declared dead). Cost: one iteration (a slow tool call) must stay under the lease, or the executor is fenced while healthy. Fencing makes that safe, not free. |
| 43 | Heartbeats are throttled to one per interval (0.5 s, an assumption). | Write on every tick. | An idle executor ticks up to 50 times a second (1 / its 0.02 s poll interval, arithmetic not a measurement); each heartbeat is a write on the one SQLite write lock. |
| 44 | "Needs an executor" is one SQL predicate (`created`, or `running` on a non-live executor), used to list candidates and again to guard the UPDATE (`assign_if_needed`). | List, then assign unconditionally. | A live executor can finish or pause the session between the two steps; an unconditional assign would restart it. Unit-tested. |
| 45 | With no live executor, sessions stay where they are and the epoch is not bumped. | Bump anyway. | Nothing to fence yet; burning epochs would only fence a holder that might come back. |
| 46 | Manager and executors compare timestamps from the same `time.time()`. | A logical clock or database-side time. | Fine on one machine; clock skew is a README gap. |
| 47 | `register_executor` became `heartbeat`: the first call registers. | Separate register and heartbeat. | They are the same upsert. |

## A tool call in flight when the executor dies

| # | Choice | Rejected alternative | Why |
|---|--------|----------------------|-----|
| 48 | Recovery asks the tool service for the stored result first, then retries only idempotent tools; a non-idempotent tool with no result is logged as `tool_unknown`. | Retry everything (the key dedupes); or fail the session. | Decision 3. Retrying a non-idempotent call whose effect already happened would repeat it (the tests show a double effect when this rule is removed). Failing the session throws away work the model could continue from. |
| 49 | "Outcome unknown" is its own log entry (`tool_unknown`), rendered into the model's messages by `Replay`. | Write a fake `tool_result` saying "unknown". | The log stays honest about what is known, and a later reader or audit can tell the two apart. |
| 50 | The unknown outcome is conservative: the model is also told "unknown" when the tool never ran (killed before the effect). | Distinguish by reading the effects ledger. | The executor cannot see the ledger in reality, and the model must be safe either way. The tests show both cases (0 and 1 effects). |
| 51 | Fault injection is a hook (`fault_hook(point)`) called at five named points; `kill_self_at` makes a worker SIGKILL itself there. | Sleeps and outside kills only. | Deterministic placement of a real `kill -9` at exactly the window under test. One extra test kills from outside, mid-delay, for realism. |
| 52 | Two test layers: simulated death (exception at the same points; fast, checks the rules and the model input) and a real-process table (9 kill points). | Only one of them. | The first isolates logic; the second proves the whole path with a real SIGKILL, reassignment and replay. |
| 53 | A zombie that finishes a non-idempotent call after the new executor recorded "unknown" leaves the model with "unknown" although the tool service has the result. | Reconcile later. | Safe (the zombie's own append is fenced); at worst the model is told less than is known. |

## Timers and caps

| # | Choice | Rejected alternative | Why |
|---|--------|----------------------|-----|
| 54 | The manager's reconciler ends sessions past a deadline (`expired`, `stopped`) before it assigns anything. | Separate timer loops. | One loop, one order: an overdue session is never handed to a fresh executor. |
| 55 | The active-run limit applies to `running` sessions only and is counted from admission (so queueing time counts). A stopped run has its epoch bumped. | Count from first assignment; also stop `created`. | Simple and conservative; a session stuck in `created` is a capacity problem, not a run that works too long. The bump fences an executor that is still mid-iteration (tested). This also closes the step-1 open item for manager-initiated endings. |
| 56 | The waiting expiry applies to `waiting` sessions only, uses the session's own `wait_timeout` (copied from config at admission) and does not touch executors or the epoch. | One shared deadline. | Decision 4. A waiting session holds no executor, so nothing needs fencing. Tested: a waiting session far past its run limit is left alone. |
| 57 | Both deadlines are strict: a session exactly at its deadline is still live. | Inclusive. | One consistent rule; boundary tested for both. |
| 58 | The executor enforces the step cap: when the model asks for a call beyond the cap, it logs `stopped` (reason `step_cap`) instead of the decision, and releases as `stopped`. | Log the decision, then refuse. | A logged tool decision with no result looks like a call in flight to the next executor and would be recovered (run). Not logging it keeps recovery correct. The cap allows exactly `step_cap` calls. |
| 59 | `step_count` is updated in the same statement as the log entry that makes the call (`counts_as_step`). | A separate update, or derive it only from the log. | The count the manager reads cannot disagree with the log, and it costs no extra write. |
| 60 | A `stop_reason` column (`step_cap` or `run_limit`) is set by both stop paths. | Reason only in the log. | The manager cannot write the log, so the run-limit stop would leave no reason; status must say why. |
| 61 | The waiting deadline is set by the executor when it releases the session (`release(..., wait_deadline)`). | The manager sets it later. | The executor is the one that knows the session started waiting, at one atomic transition. |

## Testing and measurement: chaos, invariants, harness

| # | Choice | Rejected alternative | Why |
|---|--------|----------------------|-----|
| 62 | Chaos uses two disturbances: SIGKILL (executor gone) and SIGSTOP for longer than the lease then SIGCONT (a real zombie that wakes up). | Kills only. | A killed process cannot write, so kills never exercise fencing. Only a frozen-then-resumed process does. |
| 63 | The effects ledger also records the session's epoch at commit time (`session_epoch`), and an invariant requires it to equal the caller's. | Check only that epochs never go backwards. | Without it, a zombie's effect that landed after the epoch bump looked legitimate afterwards. With it, removing the tool service's in-transaction check fails chaos in 3 of 6 runs (it was 0 of 4 before). |
| 64 | A non-idempotent call also spends its delay between the effect and the stored result. | Delay before the effect only. | Real calls take time to answer. Otherwise a random kill almost never lands in the window that matters (always-retry went undetected by chaos before this). |
| 65 | The heartbeat is also sent between the sessions of one executor, not only once per sweep. | Once per tick. | Found by the demo: with several sessions per executor, a sweep outlasted the lease and healthy executors were declared dead (epochs 4 to 5 on sessions nobody killed). Tested with a fake clock. |
| 66 | Chaos expects each agent to end in its own state (`quick` done, `ask-human` expired, `poison` stopped) and demands a minimum number of kills and at least one freeze. | Only "all sessions ended". | A weaker check passes when nothing happened. |
| 67 | The invariant checker has its own tests that inject each violation. | Trust it. | A checker that cannot fail makes a green chaos run meaningless. |
| 68 | `Cluster` runs the reconcile loop in a thread with its own manager object. | A third process. | Simple; the SQLite connection belongs to one thread, so the thread builds its own. |
| 69 | Benchmarks report every run and the machine and settings; a zero-delay series sits beside the delayed one. | One headline number. | The delayed series scales only because executors sleep; the zero-delay series shows that the ceiling is elsewhere. |

## Open items (not yet decided or built)

| Item | Why it matters | Planned in |
|------|----------------|-----------|
| The human-response path is not built: nothing moves a `waiting` session back to `running`, so a waiting session can only expire | Not in the build order. If added, `advance()` must read a response before releasing, and the run deadline must restart on resume | Not planned (README gap) |
| An unexpected exception kills the whole worker and every session on it | Swallowing it would make a broken session spin silently | Not decided |
| One manager process only | Two managers could assign the same session; needs leader election | Not planned (README gap) |
