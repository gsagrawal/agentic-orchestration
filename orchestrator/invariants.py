"""End-of-run checks for the central property. Returns problems as strings; empty means healthy.

  1. every session ended (done, cancelled, expired or stopped) and holds no executor
  2. no (session, step) had its tool run more than once
  3. no log entry or effect carries a stale epoch: epochs never go backwards within a
     session, none exceeds the session's current epoch, and every effect committed while
     its caller's epoch was still the session's current one
  4. each session's log is well formed: an outcome (result or unknown) always answers the
     call just before it, for the same step, and no new call is decided while one is open.
     A call may still be open at the very end: the manager can stop a run mid-call
  5. the manager's step_count equals the number of tool calls in the log, and no session
     exceeded its step cap; a stopped session says why
"""

import sqlite3

from . import session_log
from .state_table import TERMINAL_STATES
from .tool_service import effect_counts


def check(conn: sqlite3.Connection) -> list[str]:
    problems = []
    for s in conn.execute("SELECT * FROM sessions ORDER BY id"):
        sid = s["id"]
        if s["state"] not in TERMINAL_STATES:
            problems.append(f"{sid}: not finished, state is {s['state']}")
        elif s["executor_id"] is not None:
            problems.append(f"{sid}: ended but still holds executor {s['executor_id']}")
        if s["state"] == "stopped" and s["stop_reason"] is None:
            problems.append(f"{sid}: stopped without a reason")
        if s["step_count"] > s["step_cap"]:
            problems.append(f"{sid}: {s['step_count']} steps exceeds the cap of {s['step_cap']}")
        log = session_log.read(conn, sid)
        calls_in_log = sum(1 for e in log if e["kind"] == "model" and e["payload"]["action"]["do"] == "tool")
        if calls_in_log != s["step_count"]:
            problems.append(f"{sid}: step_count is {s['step_count']} but the log has {calls_in_log} tool calls")
        effect_epochs = [r[0] for r in conn.execute(
            "SELECT epoch FROM effects WHERE session_id = ? ORDER BY id", (sid,))]
        problems += _epoch_problems(sid, s["epoch"], log, effect_epochs)
        problems += _log_shape_problems(sid, log)
    for r in conn.execute("SELECT session_id, step, epoch, session_epoch FROM effects WHERE epoch != session_epoch"):
        problems.append(f"{r['session_id']} step {r['step']}: effect by epoch {r['epoch']} "
                        f"committed while the session was at epoch {r['session_epoch']}")
    problems += [f"{sid} step {step}: tool ran {n} times" for (sid, step), n in effect_counts(conn).items() if n > 1]
    return problems


def _epoch_problems(sid: str, current: int, log: list[dict], effect_epochs: list[int]) -> list[str]:
    problems = []
    for name, epochs in (("log", [e["epoch"] for e in log]), ("effects", effect_epochs)):
        if epochs != sorted(epochs):
            problems.append(f"{sid}: {name} has an entry from an older epoch after a newer one: {epochs}")
        if epochs and epochs[-1] > current:
            problems.append(f"{sid}: {name} has epoch {epochs[-1]} beyond the session's {current}")
    return problems


def _log_shape_problems(sid: str, log: list[dict]) -> list[str]:
    problems, last_step, open_step = [], 0, None
    for e in log:
        if e["kind"] == "model" and e["payload"]["action"]["do"] == "tool":
            if open_step is not None:
                problems.append(f"{sid}: step {last_step + 1} decided while step {open_step} has no outcome")
            last_step += 1
            open_step = last_step
        elif e["kind"] in ("tool_result", "tool_unknown"):
            if open_step != e["payload"]["step"]:
                problems.append(f"{sid}: outcome for step {e['payload']['step']} but open step is {open_step}")
            open_step = None
    return problems
