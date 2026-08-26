"""Local development server (FastAPI).

Mirrors the deployed API so you can run the whole thing on your laptop with the
same UI. Runs the graph in-process (MemorySaver) and reads live progress from
the in-memory bus. Serves the config-driven UI and exposes /api/workflow so the
UI renders whatever agents/steps are defined in workflow.json.

Run:  uvicorn app.orchestrator.server:app --port 8090
"""

import asyncio
import json
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from langgraph.types import Command
from pydantic import BaseModel

from app.common.bus import bus
from app.common.config import LAST_AGENT_ID, WORKFLOW
from app.common.sink import emit, ensure_session
from app.orchestrator.graph_builder import build_graph

app = FastAPI(title="Multi-Agent Orchestrator (local)")
graph = build_graph()  # MemorySaver for local dev
WEB = Path(__file__).resolve().parent.parent.parent / "web"
_bg: set = set()


class NewSession(BaseModel):
    topic: str = "Design a serverless data pipeline on AWS"


class Decision(BaseModel):
    decision: str = "approve"
    comment: str = ""
    decisions: dict | None = None  # per-agent map for parallel-group gates


def _config(sid: str) -> dict:
    return {"configurable": {"thread_id": sid, "actor_id": "local"}}


def _spawn(coro):
    t = asyncio.create_task(coro)
    _bg.add(t)
    t.add_done_callback(_bg.discard)


async def _run(sid: str, initial=None, resume=None):
    try:
        payload = Command(resume=resume) if resume is not None else initial
        await graph.ainvoke(payload, _config(sid))
        state = await graph.aget_state(_config(sid))
        if state.next:
            return
        values = state.values or {}
        decisions = values.get("decisions") or {}
        if "deny" in decisions.values():
            overall, result = "denied", "Denied by human. Workflow halted."
        else:
            overall, result = "done", values.get("outputs", {}).get(LAST_AGENT_ID, "")
        await emit(sid, {"type": "session_status", "status": overall,
                         "result": result, "log": f"Workflow finished ({overall})"})
    except Exception as e:  # noqa: BLE001
        await emit(sid, {"type": "session_status", "status": "failed",
                         "log": f"error: {type(e).__name__}: {e}"})


@app.get("/api/workflow")
async def workflow():
    return WORKFLOW


@app.post("/api/sessions")
async def create_session(req: NewSession):
    sid = uuid.uuid4().hex[:12]
    await ensure_session(sid, req.topic)
    initial = {"topic": req.topic, "status": {}, "outputs": {}, "decisions": {}}
    _spawn(_run(sid, initial=initial))
    return {"session_id": sid}


@app.get("/api/sessions")
async def list_sessions():
    return bus.list_sessions()


@app.get("/api/sessions/{sid}")
async def get_session(sid: str):
    if not bus.exists(sid):
        raise HTTPException(404, "unknown session")
    return bus.snapshot(sid)


@app.delete("/api/sessions/{sid}")
async def delete_session(sid: str):
    bus.delete(sid)
    return {"ok": True}


@app.post("/api/sessions/{sid}/decision")
async def decide(sid: str, req: Decision):
    if not req.decisions and req.decision not in ("approve", "deny", "revise"):
        raise HTTPException(400, "decision must be 'approve', 'deny' or 'revise', "
                                 "or provide a per-agent 'decisions' map")
    resume = {"decision": req.decision, "comment": req.comment, "decisions": req.decisions}
    _spawn(_run(sid, resume=resume))
    return {"ok": True}


@app.get("/")
async def index():
    return FileResponse(WEB / "index.html")
