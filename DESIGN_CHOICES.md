# Design choices

Choices made while building, with the alternative rejected. The seven decisions
given in the brief (epoch fencing, at-least-once tools with idempotency keys,
"outcome unknown", two timers, step cap and quotas, read-only budget status,
swappable state plumbing) are implemented as specified and are not repeated here.

## Step 1: state and log

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
| 11 | `assign()` refuses terminal sessions (`SessionClosed`). | Let the caller check the state first. | A check outside the transaction races; found in self-review of step 1. |
| 12 | `synchronous=NORMAL`. | `FULL`. | Safe against `kill -9` (what the tests do) but may lose the last commits on OS crash or power loss. Chosen for speed; a real deployment would not accept this. |

Open item (decide in step 7): `append()` checks only the epoch, so the last holder can still write to a session that has just gone terminal. The terminal transition should bump the epoch so the holder is fenced.

## Step 2: tool service stub

| # | Choice | Rejected alternative | Why |
|---|--------|----------------------|-----|
| 13 | The epoch is checked twice: once up front (fast reject), once inside the transaction that commits the effect. | Check once at the start. | A zombie can pass the first check and then sleep (a tool delay) while the session is reassigned. Only the in-transaction check stops it at the moment of the effect. Tested by changing the epoch from a fault hook. |
| 14 | The stub is stronger than a real proxy: effect, result and epoch check commit atomically. Labelled as faked in `tool_service.py`. | Pretend the real proxy can do this. | It cannot for an external API. Only the idempotency key protects a request that was already forwarded. |
| 15 | "Idempotent" means the backend dedupes on the key: effect and stored result commit together. A non-idempotent tool commits the effect first and records the result in a second step, so there is a window (effect, no result). | Same atomic commit for every tool. | Without the window, "non-idempotent tool with no recorded result" (decision 3) could never occur for real, and step 6 could not be tested honestly. |
| 16 | The result of a non-idempotent call is stored even if the epoch changed during the window. | Reject it as stale. | The effect already happened; recording it is the truth. The zombie's log append is still fenced. |
| 17 | No "started" intent record. `call()` on a non-idempotent tool with no stored result runs it again; the executor must decide not to call (step 6). | Service refuses to rerun a non-idempotent key that has no result. | Keeps to the brief: the "outcome unknown" rule lives in the executor. Cost: the service alone is not safe for non-idempotent retries (documented in the module docstring). |
| 18 | A key reused with a different tool or args raises `KeyConflict`. | Silently return the stored result. | The step number is replayed deterministically from the log, so a mismatch means a bug; failing loudly beats returning a wrong result. |
| 19 | The effects ledger has no unique constraint and is append-only (triggers). | Unique on (session, step). | A unique constraint would reject the second effect instead of recording it, hiding exactly what the tests must detect. |
| 20 | `fault_hook(point)` with points `before_effect` / `after_effect`. | Sleep-and-hope timing in tests. | Deterministic: tests (and later real `kill -9`) land at exactly the window being tested. |

## Step 3: executor

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
| 29 | Resume with a tool call in flight raises `NotImplementedError` until step 6. | Retry the call (unsafe for non-idempotent tools). | Fail closed. Labelled as a placeholder in code and tests; step 6 replaces it. |
| 30 | `agent_id`/`agent_version` columns pin the definition at admission; the agent store is a JSON file. | Look up "latest" on each pickup. | Pinning is required by the architecture: edits must not change running sessions. |
| 31 | Workers are `multiprocessing` "spawn" processes. | "fork". | A forked child would inherit the parent's open SQLite connections. |

Open items from step 3 review:
- An unexpected exception (bad agent id, unknown tool, the step-6 placeholder) kills the whole worker, and every session on it. Deliberate for now: swallowing it would make a broken session spin silently. Revisit with the reconciler (step 5) and caps (step 7).
- A resumed `waiting` session would be released again immediately. Step 7 must add a human-response log entry and make `advance()` check for it before releasing.
