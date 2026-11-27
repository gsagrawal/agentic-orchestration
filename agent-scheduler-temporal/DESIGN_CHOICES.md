# Design choices: agent-scheduler-temporal

Running list. Each entry: what I chose, what I rejected.

## Step 1: tool service

1. **Everything lives in `agent-scheduler-temporal/`, a self-contained folder** (package
   `agent_scheduler_temporal`, own Makefile, pyproject and tests). Rejected: spreading it over the
   repository root (the repository is about to hold other components and docs).
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
17. **An unknown model action, or a tool the service has no metadata for, fails the session** (non
    retryable error). Both are data errors; a plain exception would make Temporal retry forever.

## Steps 4-5: session manager, signals, API

18. **Quota counts running sessions, and a slot is freed lazily.** At admission the manager asks
    Temporal which of the tenant's "active" rows have closed, then counts. Bounded by the quota.
    Rejected: an activity at the end of the workflow that writes to the manager's table (couples the
    workflow to the manager, and a timed-out workflow would never run it).
19. **Check and insert in one `BEGIN IMMEDIATE` transaction.** Two concurrent starts cannot both
    take the last slot. Rejected: check then insert (race).
20. **Ingress is faked by trusting `X-Tenant` / `X-User` headers.** ASSUMPTION: a real ingress sets
    them after authenticating. "Responder" = only the user who started the session (ASSUMPTION; a
    real system would have a responders list). The same user check covers reply, message and stop.
21. **Another tenant's session answers 404, not 403.** It must not reveal that the id exists.
22. **API codes:** unknown or other tenant 404, wrong user 403, ended session 410 ("gone"),
    reply for a step that is not open 409, accepted 202. A reply for the wrong step is refused at the
    API *and* dropped by the workflow, because the step can close between the API's check and its signal.
23. **Signal handlers only record state; the loop acts at the step boundary.** That is what makes
    "a tool call in flight is never interrupted" true by construction. Rejected: acting inside the
    handler (would race with the loop).
24. **`open_step` is set before the notification activity.** A reply that arrives while the
    notification is still being sent is kept, not lost. Rejected: set it after (reply lost in the gap).
25. **Message de-duplication is by a client-chosen `message_id` held in workflow state** and carried
    across continue-as-new.
26. **Stop while waiting for a human ends the session at once** (the wait is a step boundary).

## Step 6: crash handling

27. **Heartbeats from the event loop while the tool runs in a thread.** Heartbeat timeout 3 s,
    interval 1 s: ASSUMPTIONS for tests, not tuned. Rejected: no heartbeats (a dead worker would be
    noticed only at the tool's start-to-close timeout, 10-30 s here).
28. **Fault injection through environment variables** (`fault.py`): the worker process kills or
    freezes itself at a named point, once. Rejected: only killing from outside (cannot hit "after the
    effect, before the result" reliably). Both are tested: the outside `kill -9` while the tool service
    shows the call started, and the hooks for the exact points.
29. **Sticky-queue timeout set to 2 s in the worker.** Measured on the first crash tests: each took
    about 14.5 s of wall time with the SDK default (10 s) and about 6.5 s with 2 s. After a worker
    dies, the next workflow task for a session it cached waits that long on its private queue.
    Trade-off: a briefly slow worker loses its cache sooner.
30. **The zombie test uses SIGSTOP/SIGCONT.** The frozen worker's pending poll is also handed the
    retry (and cannot run it), so the zombie is typically fenced twice (attempts 1 and 2) before
    attempt 3 does the work. The test asserts "every zombie attempt is lower than the winner and was
    rejected", not an exact list.
31. **"Sandbox recreated" is detected in the workflow**, by remembering that a sandbox was created
    before and `ensure_sandbox` now reports `created=True`. The model sees a system message.

## Step 7: timers and caps

32. **Active-run limit = workflow execution timeout, as specified, but it is a lifetime limit.**
    Temporal counts it from the session's start, including time waiting for a human and across
    continue-as-new. So `wait_expiry_s` must be below `active_limit_s` or the wait cannot expire.
    This is a real tension in decision 9; flagged, not redesigned.
33. **A session stopped by the execution timeout runs no cleanup**, so its sandbox is not stopped by
    the workflow. A real sandbox controller has its own deadline as a hard kill (architecture doc);
    here the fake would leak. See gaps in the README.
34. **Continue-as-new at a step boundary after `continue_after_steps` steps in one run**, carrying
    history, step number, message ids and sandbox flags. The sandbox is kept (the `finally` skips
    `stop_sandbox` when continuing). Step numbers continue, so idempotency keys stay unique.
35. **Test environment per test.** A session left running by one test stalls time skipping in all
    later tests on a shared server (found when a quota test left workerless sessions). Cost: a few
    hundred ms per test.
36. **Time skipping unlocks only for the handle `start_workflow` returned.** Tests that start
    through the manager skip time explicitly with `env.sleep`.

## Step 8: chaos and measurements

37. **Chaos uses SIGKILL only.** A killed process cannot act after the kill. Zombies (SIGSTOP) are
    tested separately, in the one window the fence covers (see gaps).
38. **The chaos test checks it is not vacuous:** at least 2 kills and at least one tool call that
    needed a second attempt. A first version with short tool calls passed with zero retries.
39. **The invariant checker has its own tests that feed it broken states** (duplicate effect,
    unrecorded effect, wrong "ok", leaked sandbox...), so it is known to be able to fail.
40. **Benchmark numbers are printed by the script from the run that produced them.** One machine,
    everything shared (dev server, workers, client). No numbers are written by hand.

## Review round 1

41. **Quota rows whose workflow never started are released**, not treated as an error. The manager
    can die between inserting the row and starting the workflow; `describe` then says NOT_FOUND.
42. **A query to a session is bounded (5 s default) and answers 503.** A query is answered by a
    worker; with none running it would wait. Signals and `describe` are answered by the server and
    are not bounded.
43. **Continue-as-new also fires when Temporal suggests it** (`is_continue_as_new_suggested`), not
    only at our own `continue_after_steps`. The suggestion needs no invented threshold. Not covered
    by a test: reaching it needs a very long history.
44. **Blocking SQLite calls in activities run in threads.** The sandbox, notifier and tool service
    each held the event loop (and the heartbeat) while waiting for a lock.
45. **The fake sandbox uses one transaction per call** (it had a read-then-write race and leaked
    connections).
46. **The scripted model records what it was shown only on request** (`record=True`, tests). A
    long-lived worker would otherwise keep every request in memory.
47. **The HTTP server is a runnable module** (`server.py`) and has an integration test. Before, the
    API was only reachable through the test client.
48. **Worker handles SIGTERM and SIGINT by shutting down cleanly.** `kill -9` is still what the
    crash tests use.
49. **Requirements are pinned to the versions the tests ran with.**

## Engine interface

50. **The session manager reaches Temporal only through `SessionEngine`** (`engine.py`, six methods),
    implemented by `TemporalEngine`. You may later replace Temporal with an API call or a message
    queue on the manager side. Rejected: using the Temporal client directly in the manager (every
    quota and authorisation path would carry Temporal types and errors). Temporal-only details moved
    into the adapter: the active-run limit as the workflow execution timeout, the query timeout, the
    NOT_FOUND-to-status mapping.
51. **The interface covers the manager side only.** The workflow, activities and worker are the
    Temporal-specific part; abstracting them would mean designing a second engine we do not have.
    Rejected: a workflow-engine abstraction layer (speculative, and it would blur the guarantees
    that depend on Temporal's retries, attempt numbers and replay).
52. **The interface states its contract in prose** (engine.py): delivery of reply, message and stop
    is at-least-once and may be late or repeated; the session, not the engine, drops what is not
    for its open step. Another engine must behave the same way.
53. **`session_id` is the only handle the manager keeps.** It does not hold workflow objects.
