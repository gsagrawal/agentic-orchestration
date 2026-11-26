"""agent-executor: a stateless worker that runs the loop for the sessions assigned to it.

It holds nothing between restarts. Everything it knows comes from the session
log (replayed on pickup) and the state table (which sessions are mine, at what epoch).

One loop iteration for a session:
    1. ask the model what to do, given the messages rebuilt from the log
    2. append the model's answer to the log
    3. if it is a tool call: call the tool service with (session, step, epoch),
       then append the result to the log
    4. if it is finish / wait: hand the session back to the session manager
On pickup, a tool call that is logged but has no result was in flight when the previous
executor died; SessionRun._recover_call_in_flight decides what to do about it.
Every append and tool call carries the epoch, so a zombie is rejected at its next write.
Sessions assigned to one worker take turns, one iteration each.
"""

import multiprocessing
import time
from typing import Callable

from . import session_log, state_table
from .agent_store import AgentStore
from .db import connect
from .errors import StaleEpoch
from .fake_tools import default_tools
from .fault import kill_self_at, no_fault
from .scripted_model import ScriptedModel
from .tool_service import ToolService


# What the model is told when a tool call may or may not have taken effect.
OUTCOME_UNKNOWN = {"error": "outcome unknown: the call may or may not have taken effect"}


class Replay:
    """What the executor knows about a session, rebuilt from its log.

    The same apply() runs for replayed and for live entries, so a resumed
    session and a never-interrupted one hold identical state.
    """

    def __init__(self):
        self.messages: list[dict] = []
        self.tool_steps = 0                   # tool calls started so far; the latest call's step number
        self.call_in_flight = False           # the latest tool call is logged but has no result yet
        self.last_action: dict | None = None  # the model's latest decision

    def apply(self, kind: str, payload: dict) -> None:
        if kind == "model":
            action = payload["action"]
            self.messages.append({"role": "assistant", "content": action})
            self.last_action = action
            if action["do"] == "tool":
                self.tool_steps += 1
                self.call_in_flight = True
        elif kind == "tool_result":
            self.messages.append({"role": "tool", "step": payload["step"], "content": payload["result"]})
            self.call_in_flight = False
        elif kind == "tool_unknown":
            self.messages.append({"role": "tool", "step": payload["step"], "content": OUTCOME_UNKNOWN})
            self.call_in_flight = False


class SessionRun:
    """One session on one executor, at one epoch."""

    def __init__(self, conn, tools: ToolService, model, agents: AgentStore, session_id: str, epoch: int,
                 fault_hook: Callable[[str], None]):
        self.conn, self.tools, self.model, self.fault_hook = conn, tools, model, fault_hook
        self.session_id, self.epoch = session_id, epoch
        row = state_table.get_session(conn, session_id)
        self.agent = agents.get(row["agent_id"], row["agent_version"])  # the pinned version
        self.replay = Replay()
        for entry in session_log.read(conn, session_id):
            self.replay.apply(entry["kind"], entry["payload"])

    def advance(self) -> bool:
        """Run one loop iteration. Returns True once the session has left this executor."""
        r = self.replay
        if r.call_in_flight:
            self._recover_call_in_flight()
        elif r.last_action is None or r.last_action["do"] == "tool":
            self._decide_and_act()
        last = r.last_action["do"]
        if last in ("finish", "wait"):
            state_table.release(self.conn, self.session_id, self.epoch, "done" if last == "finish" else "waiting")
            return True
        return False

    def _decide_and_act(self) -> None:
        action = self.model.decide(self.agent, self.replay.messages)
        self._append("model", {"action": action})
        if action["do"] == "tool":
            self._call_tool(action)

    def _call_tool(self, action: dict) -> None:
        step = self.replay.tool_steps
        self.fault_hook("before_tool_call")
        result = self.tools.call(self.session_id, step, self.epoch, action["tool"], action["args"])
        self.fault_hook("after_tool_call")
        self._append("tool_result", {"step": step, "result": result})
        self.fault_hook("after_result_logged")

    def _recover_call_in_flight(self) -> None:
        """The previous executor logged this call and died before logging its result.
        Ask the tool service what happened, using the call's idempotency key:

          result stored             use it
          none, tool idempotent     run it again; running it twice is harmless
          none, tool not idempotent record "outcome unknown" for the model. Never retry:
                                    the effect may already have happened, and a second
                                    run could, say, send a second email.
        """
        action, step = self.replay.last_action, self.replay.tool_steps
        stored = self.tools.lookup(self.session_id, step, self.epoch)
        if stored is not None:
            self._append("tool_result", {"step": step, "result": stored})
        elif self.tools.is_idempotent(action["tool"]):
            self._call_tool(action)
        else:
            self._append("tool_unknown", {"step": step})

    def _append(self, kind: str, payload: dict) -> None:
        session_log.append(self.conn, self.session_id, self.epoch, kind, payload)
        self.replay.apply(kind, payload)  # only after the append succeeded


class Executor:
    def __init__(self, db_path: str, agents_path: str, executor_id: str, tool_delay: float = 0.0, model=None,
                 clock=time.time, heartbeat_interval: float = 0.5, fault_hook: Callable[[str], None] = no_fault):
        self.executor_id = executor_id
        self.conn = connect(db_path)
        self.clock = clock
        self.heartbeat_interval = heartbeat_interval  # ASSUMPTION: well under the manager's lease
        self._last_heartbeat = self.clock()
        state_table.heartbeat(self.conn, executor_id, self._last_heartbeat)  # registers us
        self.fault_hook = fault_hook
        self.tools = ToolService(db_path, default_tools(tool_delay), fault_hook)
        self.agents = AgentStore(agents_path)
        self.model = model or ScriptedModel()
        self.runs: dict[str, SessionRun] = {}

    def tick(self) -> int:
        """Give every assigned session one iteration. Returns how many sessions were assigned.

        The heartbeat is sent from here, between iterations, on purpose: it says "the loop
        is alive", not "the process exists". A single long iteration delays it.
        """
        self._heartbeat_if_due()
        assigned = dict(state_table.assigned_to(self.conn, self.executor_id))
        # Forget sessions that moved away, or that came back at a newer epoch (replay again).
        for sid in [s for s, run in self.runs.items() if assigned.get(s) != run.epoch]:
            del self.runs[sid]
        for sid, epoch in assigned.items():
            try:
                if sid not in self.runs:
                    self.runs[sid] = SessionRun(self.conn, self.tools, self.model, self.agents, sid, epoch, self.fault_hook)
                if self.runs[sid].advance():
                    del self.runs[sid]
            except StaleEpoch:
                self.runs.pop(sid, None)  # we were fenced mid-iteration; the session is not ours
        return len(assigned)

    def _heartbeat_if_due(self) -> None:
        now = self.clock()
        if now - self._last_heartbeat >= self.heartbeat_interval:
            state_table.heartbeat(self.conn, self.executor_id, now)
            self._last_heartbeat = now

    def run_forever(self, poll_interval: float = 0.02) -> None:
        while True:
            if self.tick() == 0:
                time.sleep(poll_interval)


def _worker_main(db_path: str, agents_path: str, executor_id: str, tool_delay: float,
                 heartbeat_interval: float, kill_at: str | None) -> None:
    Executor(db_path, agents_path, executor_id, tool_delay, heartbeat_interval=heartbeat_interval,
             fault_hook=kill_self_at(kill_at)).run_forever()


def start_worker(db_path: str, agents_path: str, executor_id: str, tool_delay: float = 0.0,
                 heartbeat_interval: float = 0.5, kill_at: str | None = None):
    """Start an executor as a separate OS process (so kill -9 is real). Caller must kill it.

    `kill_at` makes the worker SIGKILL itself at that fault point (see fault.py); tests only.
    """
    proc = multiprocessing.get_context("spawn").Process(
        target=_worker_main, args=(db_path, agents_path, executor_id, tool_delay, heartbeat_interval, kill_at),
        name=executor_id,
    )
    proc.start()
    return proc
