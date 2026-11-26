"""session-manager HTTP API (FastAPI).

Ingress and auth are FAKED: the caller's tenant and user arrive in the X-Tenant and X-User
headers, and are trusted. A real ingress would set them after authenticating.
"""
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from agent_scheduler_temporal.agent_store import UnknownAgent
from agent_scheduler_temporal.session_manager import (
    NotOpenStep, NotResponder, QuotaExceeded, SessionClosed, SessionManager, Unavailable, UnknownSession,
)


class StartRequest(BaseModel):
    agent_id: str
    version: int = 1
    message: str


class ReplyRequest(BaseModel):
    step: int
    text: str


class MessageRequest(BaseModel):
    message_id: str  # the client picks it; a repeated id is dropped by the workflow
    text: str


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
        except UnknownAgent:
            raise HTTPException(status_code=404, detail="unknown agent or version")
        return {"session_id": session_id}

    # Error mapping: unknown/other tenant -> 404, wrong user -> 403, ended -> 410, wrong step -> 409,
    # session did not answer in time -> 503.
    async def call(coro):
        try:
            await coro
        except UnknownSession:
            raise HTTPException(status_code=404, detail="no such session")
        except NotResponder:
            raise HTTPException(status_code=403, detail="not the user who started this session")
        except SessionClosed:
            raise HTTPException(status_code=410, detail="session has ended")
        except NotOpenStep as e:
            raise HTTPException(status_code=409, detail=str(e))
        except Unavailable as e:
            raise HTTPException(status_code=503, detail=str(e))
        return {"accepted": True}

    @app.post("/sessions/{session_id}/replies", status_code=202)
    async def post_reply(session_id: str, body: ReplyRequest, x_tenant: str = Header(), x_user: str = Header()):
        return await call(manager.reply(x_tenant, x_user, session_id, body.step, body.text))

    @app.post("/sessions/{session_id}/messages", status_code=202)
    async def post_message(session_id: str, body: MessageRequest, x_tenant: str = Header(), x_user: str = Header()):
        return await call(manager.message(x_tenant, x_user, session_id, body.message_id, body.text))

    @app.post("/sessions/{session_id}/stop", status_code=202)
    async def post_stop(session_id: str, x_tenant: str = Header(), x_user: str = Header()):
        return await call(manager.stop(x_tenant, x_user, session_id))

    return app
