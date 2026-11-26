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
