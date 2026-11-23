from __future__ import annotations

import json
import queue
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app_models import ApprovalRequest, ChatRequest, ResumeRequest, SessionResponse, SessionState
from checkpoint_store import CheckpointStore
from orchestration import (
    _trace_emitter,
    create_session,
    handle_approval,
    handle_chat,
    handle_resume,
    response_for_session,
)


app = FastAPI(
    title="TripFlow",
    description="A transparent multi-agent travel planning workflow.",
)
static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")

sessions: dict[str, SessionState] = {}
checkpoint_store = CheckpointStore()


def _sse_frame(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n"


def _make_stream(operation) -> StreamingResponse:
    """Run `operation()` in a worker thread and stream trace events via SSE."""
    q: queue.Queue = queue.Queue()

    def emit(event) -> None:
        q.put(("trace", {
            "ts": event.ts,
            "step": event.step,
            "message": event.message,
            "data": event.data,
        }))

    def run() -> None:
        token = _trace_emitter.set(emit)
        try:
            result = operation()
            q.put(("done", result))
        except Exception as e:  # noqa: BLE001
            q.put(("error", {"message": str(e) or e.__class__.__name__}))
        finally:
            _trace_emitter.reset(token)

    threading.Thread(target=run, daemon=True).start()

    def generate():
        while True:
            kind, data = q.get()
            if kind == "trace":
                yield _sse_frame("trace", data)
            elif kind == "done":
                yield _sse_frame("done", data.model_dump())
                break
            elif kind == "error":
                yield _sse_frame("error", data)
                break

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/")
def index() -> FileResponse:
    return FileResponse(static_dir / "index.html")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/session", response_model=SessionResponse)
def create_workflow_session() -> SessionResponse:
    session = create_session()
    sessions[session.session_id] = session
    return response_for_session(session)


@app.get("/api/session/{session_id}", response_model=SessionResponse)
def get_workflow_session(session_id: str) -> SessionResponse:
    session = sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return response_for_session(session)


@app.post("/api/chat", response_model=SessionResponse)
def chat(payload: ChatRequest) -> SessionResponse:
    session = sessions.get(payload.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return handle_chat(session, payload.message, checkpoint_store)


@app.post("/api/chat/stream")
async def chat_stream(payload: ChatRequest) -> StreamingResponse:
    session = sessions.get(payload.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return _make_stream(lambda: handle_chat(session, payload.message, checkpoint_store))


@app.post("/api/approval", response_model=SessionResponse)
def approval(payload: ApprovalRequest) -> SessionResponse:
    session = sessions.get(payload.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return handle_approval(session, payload.approve, checkpoint_store)


@app.post("/api/approval/stream")
async def approval_stream(payload: ApprovalRequest) -> StreamingResponse:
    session = sessions.get(payload.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return _make_stream(lambda: handle_approval(session, payload.approve, checkpoint_store))


@app.post("/api/resume", response_model=SessionResponse)
def resume(payload: ResumeRequest) -> SessionResponse:
    session = sessions.get(payload.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return handle_resume(session, checkpoint_store)