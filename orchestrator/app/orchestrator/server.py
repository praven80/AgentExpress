"""Local development server (FastAPI).

Mirrors the deployed API so you can run the whole thing on your laptop with the
same UI. Runs the graph in-process (MemorySaver) and reads live progress from
the in-memory bus. Serves the config-driven UI and exposes /api/workflow so the
UI renders whatever agents/steps are defined in workflow.json.

Run:  uvicorn app.orchestrator.server:app --port 8090

The AgentCore-only endpoints (/evaluate, /insights) answer with a clear
"not available locally" message rather than 404, so the UI behaves predictably:
evaluations and insights read CloudWatch/AgentCore APIs that only exist once
deployed.
"""

import asyncio
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from langgraph.types import Command
from pydantic import BaseModel, Field

from app.common.bus import bus
from app.common.config import DEFAULT_TOPIC, LAST_AGENT_ID, WORKFLOW
from app.common.sink import emit, ensure_session
from app.orchestrator.graph_builder import build_graph, group_rerun_plan, rerun_plan

app = FastAPI(title="Multi-Agent Orchestrator (local)")
graph = build_graph()  # MemorySaver for local dev
WEB = Path(__file__).resolve().parent.parent.parent / "web"
_bg: set = set()


class NewSession(BaseModel):
    model_config = {"populate_by_name": True}
    # Default from workflow.json (ui.defaultTopic), not a literal: the sample's
    # topic has nothing to do with a customer's use case, and a hardcoded one here
    # silently seeds a run with it.
    topic: str = DEFAULT_TOPIC
    # Optional grouping key that scopes long-term memory. Bound from the camelCase
    # JSON key the UI sends, exposed as snake_case in Python.
    subject_id: str | None = Field(default=None, alias="subjectId")


class Decision(BaseModel):
    decision: str = "approve"
    comment: str = ""
    decisions: dict | None = None  # per-agent map for parallel-group gates


class Rerun(BaseModel):
    model_config = {"populate_by_name": True}
    agent_id: str | None = Field(default=None, alias="agentId")
    comment: str = ""
    agents: list[dict] | None = None   # subset of a parallel stage


def _config(sid: str) -> dict:
    return {"configurable": {"thread_id": sid, "actor_id": "local"}}


def _spawn(coro):
    t = asyncio.create_task(coro)
    _bg.add(t)
    t.add_done_callback(_bg.discard)


async def _rewind(sid: str, rerun: dict):
    """Local mirror of runtime._rewind: rewind so the chosen agent(s) re-run and
    cascade downstream, injecting the reviewer comment(s) as feedback."""
    agents = rerun.get("agents")
    if agents:
        fb_in = {a["agent_id"]: (a.get("comment", "") or "") for a in agents if a.get("agent_id")}
        plan = group_rerun_plan(list(fb_in.keys()))
        downstream = plan["downstream_agents"]
        gid, subset = plan["group_id"], plan["subset"]
        fb = {a: "" for a in downstream}
        fb.update(fb_in)
        values = {"decisions": {gid: "revise"}, "group_rerun": {gid: subset}, "feedback": fb}
        await graph.aupdate_state(_config(sid), values, as_node=plan["as_node"])
        await emit(sid, {"type": "session_status", "status": "running", "result": "",
                         "log": f"Re-running {', '.join(subset)} in {gid}"})
        for a in downstream:
            await emit(sid, {"type": "node_status", "node": a, "status": "pending"})
        return

    plan = rerun_plan(rerun["agent_id"])
    downstream = plan["downstream_agents"]
    values: dict = {}
    if plan["decision_key"]:
        values["decisions"] = {plan["decision_key"]: "approve"}
    fb = {a: "" for a in downstream}
    fb[rerun["agent_id"]] = rerun.get("comment", "") or ""
    values["feedback"] = fb
    await graph.aupdate_state(_config(sid), values, as_node=plan["as_node"])
    await emit(sid, {"type": "session_status", "status": "running", "result": "",
                     "log": f"Rerun from {rerun['agent_id']} (step {plan['step_index']})"})
    for a in downstream:
        await emit(sid, {"type": "node_status", "node": a, "status": "pending"})


async def _run(sid: str, initial=None, resume=None, rerun=None):
    try:
        if rerun is not None:
            await _rewind(sid, rerun)
            payload = None
        else:
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
    initial = {"topic": req.topic, "subject_id": req.subject_id or "",
               "status": {}, "outputs": {}, "decisions": {}}
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


@app.post("/api/sessions/{sid}/rerun")
async def rerun(sid: str, req: Rerun):
    """Rewind to an agent (or a subset of a parallel stage) and re-run it plus
    everything downstream, with the reviewer comment injected as feedback.
    Downstream HITL gates re-pause."""
    if not bus.exists(sid):
        raise HTTPException(404, "unknown session")
    if req.agents:
        agents = [{"agent_id": a.get("agent_id") or a.get("agentId"),
                   "comment": a.get("comment", "")}
                  for a in req.agents if (a.get("agent_id") or a.get("agentId"))]
        try:
            group_rerun_plan([a["agent_id"] for a in agents])
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        _spawn(_run(sid, rerun={"agents": agents}))
        return {"ok": True}
    if not req.agent_id:
        raise HTTPException(400, "provide 'agentId' or a non-empty 'agents' list")
    try:
        rerun_plan(req.agent_id)  # validate target
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    _spawn(_run(sid, rerun={"agent_id": req.agent_id, "comment": req.comment}))
    return {"ok": True}


# --- AgentCore-only endpoints (deployed feature parity for the UI) ---------
# Evaluations and Insights read the AgentCore Evaluate / BatchEvaluation APIs and
# CloudWatch traces, which only exist for a deployed runtime. Locally they answer
# with an explicit note so the UI shows why instead of erroring.

@app.post("/api/sessions/{sid}/evaluate")
async def evaluate(sid: str):
    return {"ok": False,
            "error": "AgentCore Evaluations needs a deployed runtime (it scores "
                     "CloudWatch traces / persisted telemetry). Deploy, then use "
                     "the Evaluate button."}


@app.post("/api/insights/run")
async def insights_run():
    return {"ok": False,
            "error": "AgentCore Insights needs a deployed runtime with CloudWatch "
                     "Transaction Search enabled."}


@app.get("/api/insights")
async def insights_latest():
    return {"status": "none",
            "note": "Insights is available once deployed (needs runtime traces in CloudWatch)."}


@app.get("/")
async def index():
    return FileResponse(WEB / "index.html")
