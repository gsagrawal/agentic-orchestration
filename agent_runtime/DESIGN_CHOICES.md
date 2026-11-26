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
