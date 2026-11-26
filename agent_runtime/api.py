"""session-manager HTTP API (FastAPI).

Ingress and auth are FAKED: the caller's tenant and user arrive in the X-Tenant and X-User
headers, and are trusted. A real ingress would set them after authenticating.
"""
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from agent_runtime.session_manager import QuotaExceeded, SessionManager


class StartRequest(BaseModel):
    agent_id: str
    version: int = 1
    message: str


def create_app(manager: SessionManager) -> FastAPI:
    app = FastAPI(title="agent session manager")

    @app.post("/sessions", status_code=201)
    async def start_session(
        body: StartRequest,
        x_tenant: str = Header(),
        x_user: str = Header(),
    ):
        try:
            session_id = await manager.start(x_tenant, x_user, body.agent_id, body.version, body.message)
        except QuotaExceeded as e:
            raise HTTPException(status_code=429, detail=str(e))
        except KeyError:
            raise HTTPException(status_code=404, detail="unknown agent or version")
        return {"session_id": session_id}

    return app
