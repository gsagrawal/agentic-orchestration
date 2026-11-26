"""Chaos harness: N sessions across M real worker processes while workers are SIGKILLed at random
times and replaced. Everything the invariants need is read back from the shared SQLite files.

Used by tests/test_agent_runtime_chaos.py and scripts/runtime_bench.py. Needs a Temporal dev server.
"""
import asyncio
import random
import signal
import time
from dataclasses import dataclass
from datetime import timedelta

from temporalio.client import Client
from temporalio.service import RPCError

from agent_runtime.harness import WorkerProcesses
from agent_runtime.models import AgentVersion, ReplySignal, SessionInput, SessionResult
from agent_runtime.notifier import FakeNotifier
from agent_runtime.sandbox import FakeSandboxController
from agent_runtime.tool_service import ToolService
from agent_runtime.tools import FakeTools
from agent_runtime.workflow import AgentSession

ALL_GRANTS = ["read", "write", "code", "email"]


def _agent(name, script, **kw) -> AgentVersion:
    return AgentVersion(name, 1, list(FakeTools.SPECS.values()), ALL_GRANTS, kw.pop("step_cap", 30), script, **kw)


def _t(tool, **args):
    return {"do": "tool", "tool": tool, "args": args}


FINISH = {"do": "finish"}
# One of each kind of session. Short continue_after_steps on some, so continue-as-new happens under kills.
CHAOS_AGENTS: dict[str, AgentVersion] = {
    "idempotent": _agent("idempotent", [_t("set_value"), _t("lookup"), _t("set_value"), FINISH], continue_after_steps=2),
    "non-idempotent": _agent("non-idempotent", [_t("send_email"), _t("lookup"), _t("send_email"), FINISH]),
    "code": _agent("code", [_t("run_code"), _t("lookup"), _t("run_code"), FINISH], continue_after_steps=3),
    "asks-and-gets-answer": _agent("asks-and-gets-answer", [_t("lookup"), {"do": "ask_human", "question": "ok?"}, _t("send_email"), FINISH]),
    "asks-and-expires": _agent("asks-and-expires", [{"do": "ask_human", "question": "ok?"}, FINISH], wait_expiry_s=3.0),
    "poison": _agent("poison", [_t("lookup")], loop=True, step_cap=15),
}
KINDS = list(CHAOS_AGENTS)


@dataclass
class ChaosReport:
    results: dict[str, SessionResult]
    agent_of: dict[str, AgentVersion]
    kills: int
    elapsed_s: float
    tools: ToolService
    sandbox: FakeSandboxController
    notifier: FakeNotifier


async def _answer_when_asked(client: Client, sid: str, step: int) -> None:
    """Replies once the session is waiting at `step`. A query needs a live worker, so it may fail
    while workers are being killed; just try again."""
    handle = client.get_workflow_handle_for(AgentSession.run, sid)
    while True:
        try:
            if await handle.query(AgentSession.open_step, rpc_timeout=timedelta(seconds=5)) == step:
                await handle.signal(AgentSession.reply, ReplySignal(step, "yes"))
                return
        except RPCError:
            pass
        await asyncio.sleep(0.3)


async def run_chaos(
    client: Client, address: str, data_dir: str, n_sessions: int, n_workers: int, seed: int,
    tool_delay_s: float = 0.3, kill_gap_s: tuple[float, float] = (2.0, 5.0), timeout_s: float = 300,
) -> ChaosReport:
    rng = random.Random(seed)
    workers = WorkerProcesses(address, data_dir)
    live = [workers.spawn(tool_delay=tool_delay_s) for _ in range(n_workers)]
    start = time.monotonic()
    handles, agent_of, answerers = {}, {}, []
    try:
        for i in range(n_sessions):
            agent = CHAOS_AGENTS[KINDS[i % len(KINDS)]]
            sid = f"chaos-{seed}-{i}"
            agent_of[sid] = agent
            handles[sid] = await client.start_workflow(
                AgentSession.run, SessionInput(sid, "tenant-1", agent, "go"), id=sid, task_queue=workers.task_queue
            )
            if agent.agent_id == "asks-and-gets-answer":
                answerers.append(asyncio.create_task(_answer_when_asked(client, sid, step=1)))

        kills = 0
        all_done = asyncio.ensure_future(asyncio.gather(*(h.result() for h in handles.values())))
        while not all_done.done():
            await asyncio.wait({all_done}, timeout=rng.uniform(*kill_gap_s))
            if all_done.done():
                break
            for t in answerers:
                if t.done() and t.exception():  # a crashed answerer would otherwise look like a hung session
                    raise t.exception()
            victim = rng.choice(live)
            victim.send_signal(signal.SIGKILL)
            victim.wait()
            live.remove(victim)
            live.append(workers.spawn(tool_delay=tool_delay_s))  # replace it: M workers again
            kills += 1
            if time.monotonic() - start > timeout_s:
                raise TimeoutError(f"{n_sessions} sessions did not finish in {timeout_s} s")
        results = dict(zip(handles, all_done.result()))
    finally:
        for t in answerers:
            t.cancel()
        workers.cleanup()
    return ChaosReport(
        results, agent_of, kills, time.monotonic() - start,
        ToolService(f"{data_dir}/tools.db"), FakeSandboxController(f"{data_dir}/sandbox.db"), FakeNotifier(f"{data_dir}/notifier.db"),
    )
