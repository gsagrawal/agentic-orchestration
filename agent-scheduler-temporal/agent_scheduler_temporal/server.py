"""Run the session-manager HTTP API.

    python -m agent_scheduler_temporal.server --address localhost:7233 --db sessions.db

Ingress and auth are faked: the API trusts the X-Tenant and X-User headers (see api.py).
"""
import argparse
import asyncio

import uvicorn
from temporalio.client import Client

from agent_scheduler_temporal.api import create_app
from agent_scheduler_temporal.models import TASK_QUEUE
from agent_scheduler_temporal.session_manager import Quotas, SessionManager
from agent_scheduler_temporal.temporal_engine import TemporalEngine


async def serve(address: str, db_path: str, host: str, port: int, max_active: int, task_queue: str, continue_after_steps: int) -> None:
    client = await Client.connect(address)
    manager = SessionManager(TemporalEngine(client, task_queue, continue_after_steps=continue_after_steps), db_path, Quotas(default_max_active=max_active))
    await uvicorn.Server(uvicorn.Config(create_app(manager), host=host, port=port)).serve()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--address", default="localhost:7233")
    p.add_argument("--db", default="sessions.db")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--max-active", type=int, default=5, help="default per-tenant limit on running sessions")
    p.add_argument("--task-queue", default=TASK_QUEUE)
    p.add_argument("--continue-after-steps", type=int, default=0, help="executor setting; 0 = only when Temporal suggests it")
    a = p.parse_args()
    asyncio.run(serve(a.address, a.db, a.host, a.port, a.max_active, a.task_queue, a.continue_after_steps))
