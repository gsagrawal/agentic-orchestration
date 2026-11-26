# Agent scheduler and runtime (MVP)

Admits agent sessions, assigns each to exactly one executor, keeps it alive, and
recovers it when an executor dies. Design background: `docs/service-architecture.md`.
Choices and rejected alternatives: `DESIGN_CHOICES.md`.

> Work in progress. This README grows with each build step; the full
> real / faked / not-built sections arrive with the final step.

## Layout (one file per component)

| File | Component |
|------|-----------|
| `orchestrator/db.py` | SQLite connection, schema, transactions |
| `orchestrator/state_table.py` | session-manager: the session state table |
| `orchestrator/session_log.py` | session log with epoch-checked appends |
| `orchestrator/errors.py` | shared exceptions |

## Run

```
make test
```

## Limit, stated up front

SQLite on one machine proves the *logic* (fencing, replay, idempotency), not
distributed behaviour. With Postgres or a real queue you would also have to deal
with row locks and lock timeouts, leases under clock skew, and network
partitions, none of which this code exercises. Also, `synchronous=NORMAL` survives `kill -9` of a process
but not necessarily an OS crash or power loss.
