# Design choices: Temporal runtime (`agent_runtime/`)

Running list. Each entry: what I chose, what I rejected.

## Step 1: tool service

1. **New package `agent_runtime/`; `orchestrator/` untouched.** The merged `orchestrator/` is a
   different design (state table + reconciler). Rejected: replacing it (discards merged work
   without being asked). Owner decision, recorded in the PR.
2. **Plain Python class over a SQLite file, not an HTTP service yet.** Several worker processes
   share the file. Rejected: HTTP now (no step needs it before the activities exist).
3. **Check-and-write inside `BEGIN IMMEDIATE`, one connection per call.** Makes fencing and the
   started marker atomic across processes. Rejected: Python-level locks (do not span processes).
4. **Two transactions per call, tool runs between them.** The marker is committed before the tool
   runs, so a kill during the run leaves a marker. Rejected: one transaction (no marker would
   survive a crash, defeating "outcome unknown").
5. **A result is refused if a newer attempt has taken over (`StaleAttempt` on record).** A zombie
   that finishes late cannot overwrite. Rejected: last-writer-wins.
6. **Equal attempt is allowed** (`attempt >= highest`), so a repeated call of the same attempt
   is not mistaken for a zombie. Rejected: strictly-greater (would reject legitimate repeats).
7. **Idempotent fake tools dedupe in the ledger by key (`record_once`).** This models a backend
   that honours the idempotency key; the ledger then shows what really happened. Rejected:
   a UNIQUE constraint on the ledger (would make the "no duplicate effects" invariant true by
   construction, so it could never fail).
8. **Tool metadata is a `ToolSpec` dataclass (type, idempotent, timeout, grant).** Grants and
   timeouts are not enforced by the tool service; the activity and workflow will use them later.

## Steps 2-3: workflow, activities, worker

9. **Grant check inside the workflow, no activity.** The agent version is workflow input, so the
   check is deterministic data lookup. Rejected: an activity per check (extra round trip, no gain).
10. **Tool metadata in two places on purpose:** the workflow uses type/timeout/grant from the
    agent version; the tool service uses its own idempotent flag. The tool service is the one that
    acts on idempotency, so it must not trust the caller. Rejected: sending the flag in the call.
11. **Attempt = `activity.info().attempt`.** Temporal persists it and raises it on every retry,
    including after a worker death. Rejected: own counter (would need storage).
12. **A stale attempt is a non-retryable error.** A fenced attempt can never succeed by retrying.
13. **Unbounded retries with capped backoff for activities.** Tool calls are at-least-once; the
    active-run limit (step 7) is what bounds a stuck session. Rejected: max attempts (would fail
    sessions on a long outage).
14. **Blocking tool service runs via `asyncio.to_thread`.** Rejected: sync activity with a thread
    pool (more worker config for no gain at this size).
15. **Scripted model reads the step number only.** Keeps it deterministic. Rejected: a model that
    inspects history (not needed until step 6 asks for the model's input to be checked).
16. **`finally` stops the sandbox.** Does not yet handle workflow cancellation; revisited with the
    stop signal (step 5).
17. **Demo/test targets are named `*-runtime`** because `make test` and `make demo` still run the
    older `orchestrator/` code. To be unified if that code is removed.
