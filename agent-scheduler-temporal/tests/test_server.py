"""The HTTP server as a real process: start a session over HTTP, a worker process runs it."""
import socket
import subprocess
import sys

import httpx
import pytest

from tests.helpers import wait_until

pytestmark = pytest.mark.integration


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def test_start_a_session_over_http_and_it_runs_to_the_end(dev_env, workers, tmp_path):
    port = free_port()
    server = subprocess.Popen([
        sys.executable, "-m", "agent_scheduler_temporal.server", "--address", workers.address,
        "--db", str(tmp_path / "sessions.db"), "--port", str(port), "--task-queue", workers.task_queue,
    ])
    workers.procs.append(server)  # cleaned up with the workers
    workers.spawn()
    headers = {"X-Tenant": "acme", "X-User": "u1"}
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as api:
        async def up():
            try:
                return (await api.post("/sessions", json={"agent_id": "nope", "message": "x"}, headers=headers)).status_code == 404
            except httpx.TransportError:
                return False

        await wait_until(up, timeout=30)  # the server answers (unknown agent is a 404)
        r = await api.post("/sessions", json={"agent_id": "setter", "message": "go"}, headers=headers)
        assert r.status_code == 201
        session_id = r.json()["session_id"]

    handle = dev_env.client.get_workflow_handle(session_id)
    result = await handle.result()
    assert result["status"] == "finished"  # untyped handle: the raw JSON result
