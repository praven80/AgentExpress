"""AgentCore Runtime entrypoint.

Long-running / async model: each invocation registers an async task
(app.add_async_task) and runs the workflow in a background task, returning to
the caller immediately. While a task is registered, /ping reports HealthyBusy,
keeping the session alive up to the 8-hour max lifetime. Progress is written to
DynamoDB (sink) for the UI; HITL pauses persist via AgentCore Memory and resume
on a later invocation.

Invoke contract:
  {"action": "start",  "topic": "...", "session_id": "..."(optional)}
  {"action": "resume", "session_id": "...", "decision": "approve"|"deny"|"revise",
   "comment": "..."(feedback, used on revise)}
Both return immediately; clients poll the status store for progress.
"""
# ruff: noqa: E402

import asyncio
import uuid

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from langgraph.types import Command

from app.common.config import LAST_AGENT_ID, MEMORY_ID, REGION
from app.orchestrator.graph_builder import build_graph
from app.common.sink import emit, ensure_session, is_cancelled, WorkflowCancelled

if not MEMORY_ID:
    raise RuntimeError("MEMORY_ID environment variable is required")

from langgraph_checkpoint_aws import AgentCoreMemorySaver  # noqa: E402

graph = build_graph(AgentCoreMemorySaver(MEMORY_ID, region_name=REGION))
app = BedrockAgentCoreApp()

ACTOR_ID = "orchestrator"
_bg_tasks: set = set()


def _config(session_id: str) -> dict:
    return {"configurable": {"thread_id": session_id, "actor_id": ACTOR_ID}}


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


async def _finalize(session_id: str) -> None:
    state = await graph.aget_state(_config(session_id))
    if state.next:
        return  # paused at a HITL gate; resume will continue
    values = state.values or {}
    decisions = values.get("decisions") or {}
    if "deny" in decisions.values():
        overall, result = "denied", "Denied by human. Workflow halted."
    else:
        overall, result = "done", values.get("outputs", {}).get(LAST_AGENT_ID, "")
    await emit(session_id, {"type": "session_status", "status": overall,
                            "result": result, "log": f"Workflow finished ({overall})"})


async def _run(session_id: str, initial=None, resume=None, user: str = "") -> None:
    task_id = app.add_async_task("workflow")
    import time as _t
    _burst_start = _t.perf_counter()
    try:
        payload = Command(resume=resume) if resume is not None else initial
        await graph.ainvoke(payload, _config(session_id))
        await _finalize(session_id)
    except WorkflowCancelled:
        await emit(session_id, {"type": "session_status", "status": "cancelled",
                                "result": "Stopped by user.", "log": "Workflow cancelled by user"})
    except Exception as e:  # noqa: BLE001
        # A node may raise WorkflowCancelled wrapped by the graph runtime; if the
        # user asked to stop, report it as cancelled rather than a failure.
        if is_cancelled(session_id):
            await emit(session_id, {"type": "session_status", "status": "cancelled",
                                    "result": "Stopped by user.", "log": "Workflow cancelled by user"})
        else:
            await emit(session_id, {"type": "session_status", "status": "failed",
                                    "log": f"Workflow error: {type(e).__name__}: {e}"})
    finally:
        # Record the AgentCore Runtime compute consumed by this active burst
        # (best-effort; isolated in app/observability).
        try:
            from app.observability import meter
            meter.record_session_compute(
                session_id=session_id, user=user,
                active_seconds=_t.perf_counter() - _burst_start,
            )
        except Exception:  # noqa: BLE001
            pass
        app.complete_async_task(task_id)


@app.entrypoint
async def invoke(payload, context=None):
    action = payload.get("action", "start")

    if action == "start":
        session_id = payload.get("session_id") or uuid.uuid4().hex[:12]
        topic = payload.get("topic", "Design a serverless data pipeline on AWS")
        user = payload.get("user", "")
        await ensure_session(session_id, topic)
        initial = {"topic": topic, "user": user,
                   "status": {}, "outputs": {}, "decisions": {}}
        _spawn(_run(session_id, initial=initial, user=user))
        return {"session_id": session_id, "status": "started"}

    if action == "resume":
        session_id = payload.get("session_id")
        if not session_id:
            return {"error": "resume requires 'session_id'"}
        decision = payload.get("decision", "approve")
        # `decisions` (a per-agent map) is used by parallel-group gates; when it
        # is present the single `decision` is not required to be one of the three.
        decisions = payload.get("decisions")
        if not decisions and decision not in ("approve", "deny", "revise"):
            return {"error": "decision must be 'approve', 'deny' or 'revise'"}
        resume = {"decision": decision, "comment": payload.get("comment", ""),
                  "decisions": decisions}
        _spawn(_run(session_id, resume=resume, user=payload.get("user", "")))
        return {"session_id": session_id, "status": "resuming"}

    return {"error": f"unknown action '{action}'; use 'start' or 'resume'"}


if __name__ == "__main__":
    app.run()
