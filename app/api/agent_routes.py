"""Durable task APIs and resumable SSE; GET never creates another task."""
import asyncio
import json
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from app.agent.contracts import StartRequest, ApprovalRequest, dump
from app.core.errors import AppError
from app.core.identity import Identity, get_identity

router = APIRouter()

def service(request):
    if request.app.state.persistent_agent is None:
        raise AppError("AGENT_NOT_CONFIGURED", "Set KNOWLEDGEOPS_AGENT_DATABASE_URL", 503)
    return request.app.state.persistent_agent

def public(state):
    return {k: v for k, v in state.items() if k not in {"messages", "seen"}}

@router.post("/agent", status_code=202)
async def start(body: StartRequest, request: Request, identity: Identity = Depends(get_identity)):
    agent = service(request)
    state = await asyncio.to_thread(agent.repo.create, identity, body.message, body.request_key, request.state.request_id)
    agent.launch(state["run_id"], identity)
    return public(state)

@router.get("/agent/{run_id}")
async def get_run(run_id: str, request: Request, identity: Identity = Depends(get_identity)):
    state = await asyncio.to_thread(service(request).repo.get, run_id, identity)
    if state.get("conversation_id"):
        await asyncio.to_thread(request.app.state.conversations.sync_agent_turns, state["conversation_id"], identity, False)
    return public(state)

@router.post("/agent/{run_id}/resume", status_code=202)
async def resume(run_id: str, request: Request, identity: Identity = Depends(get_identity)):
    agent = service(request)
    state = await asyncio.to_thread(agent.repo.get, run_id, identity)
    agent.launch(run_id, identity)
    return public(state)

@router.post("/agent/{run_id}/approval")
async def approve(run_id: str, body: ApprovalRequest, request: Request, identity: Identity = Depends(get_identity)):
    agent = service(request)
    state = await asyncio.to_thread(agent.repo.approve, run_id, identity, body)
    agent.launch(run_id, identity)
    return public(state)

@router.post("/agent/{run_id}/cancel")
async def cancel(run_id: str, request: Request, identity: Identity = Depends(get_identity)):
    agent = service(request)
    state = await asyncio.to_thread(agent.repo.cancel, run_id, identity)
    job = agent.jobs.get(run_id)
    if job:
        job.cancel()
    return public(state)

@router.get("/agent/{run_id}/events")
async def events(run_id: str, request: Request, after: int = Query(0, ge=0), identity: Identity = Depends(get_identity)):
    agent = service(request)
    await asyncio.to_thread(agent.repo.get, run_id, identity)
    header = request.headers.get("last-event-id")
    if header:
        try:
            after = max(after, int(header))
        except ValueError:
            raise AppError("INVALID_EVENT_ID", "Last-Event-ID must be an integer", 422)
    async def stream():
        cursor = after
        while not await request.is_disconnected():
            state = await asyncio.to_thread(agent.repo.get, run_id, identity)
            for item in state["events"]:
                if item["id"] > cursor:
                    yield f'id: {item["id"]}\nevent: {item["type"]}\ndata: {dump(item)}\n\n'
                    cursor = item["id"]
            if state["status"] != "running":
                return
            yield ": heartbeat\n\n"
            await asyncio.sleep(.2)
    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
