"""Generic wrappers that turn agents and HITL gates into LangGraph nodes.

Agent authors never touch this — it handles status emission, output storage,
and the interrupt/resume mechanics uniformly for every agent.
"""

from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt

from app.common import clock
from app.common.base import Agent
from app.common.context import AgentContext
from app.common.sink import WorkflowCancelled, emit, is_cancelled


def make_agent_node(agent: Agent):
    """Wrap an Agent into a node: emit running -> run() -> emit done + store output.

    Each run is also recorded as a numbered version in the `history` channel,
    together with the reviewer comment that triggered it (empty on the first
    run), so the UI can show v1, then "review comment -> v2", and so on.
    """

    async def node(state: dict, config: RunnableConfig) -> dict:
        ctx = AgentContext(agent, state, config)
        if is_cancelled(ctx.session_id):
            raise WorkflowCancelled(ctx.session_id)
        await emit(ctx.session_id, {"type": "node_status", "node": agent.id,
                                    "status": "running", "log": f"{agent.name} started"})
        out = await agent.run(ctx)

        # Append this run as the next version. The comment is the reviewer
        # feedback that caused a re-run (empty on the initial run).
        prior = list((state.get("history") or {}).get(agent.id, []))
        version = len(prior) + 1
        comment = (state.get("feedback") or {}).get(agent.id, "") or ""
        record = {"version": version, "at": clock.now_str(),
                  "comment": comment, "output": out}
        history = prior + [record]

        await emit(ctx.session_id, {"type": "node_status", "node": agent.id,
                                    "status": "done", "output": out, "history": history,
                                    "log": f"{agent.name} complete (v{version})"})
        return {"status": {agent.id: "done"}, "outputs": {agent.id: out},
                "history": {agent.id: history}}

    return node


def make_gate_node(agent_id: str, agent_name: str):
    """A generic human-in-the-loop gate placed after `agent_id`.

    approve -> continue; deny -> halt (routing to END handled by the builder);
    revise -> record feedback and loop back to re-run `agent_id` (the router
    returns `agent_id`). Kept separate from the agent node so approve/deny
    resume only this cheap gate, while revise re-runs the agent with feedback.
    """

    async def gate(state: dict, config: RunnableConfig) -> dict:
        sid = config["configurable"]["thread_id"]
        if is_cancelled(sid):
            raise WorkflowCancelled(sid)
        out = (state.get("outputs") or {}).get(agent_id, state.get("topic", ""))
        question = f"Review {agent_name} ({agent_id}) output: {str(out)[:120]}"
        await emit(sid, {"type": "hitl_request", "node": agent_id, "question": question,
                         "log": f"Awaiting human review after {agent_id}"})

        # Resume payload is {"decision": approve|deny|revise, "comment": str}.
        # A bare string is accepted for backward compatibility.
        resumed = interrupt({"node": agent_id, "question": question})  # pauses here
        if isinstance(resumed, dict):
            decision = resumed.get("decision", "approve")
            comment = resumed.get("comment", "") or ""
        else:
            decision, comment = (resumed or "approve"), ""

        await emit(sid, {"type": "hitl_resolved", "node": agent_id,
                         "log": f"Human decision ({agent_id}): {decision}"})

        if decision == "approve":
            await emit(sid, {"type": "node_status", "node": agent_id, "status": "done",
                             "log": f"{agent_id} approved; continuing"})
            return {"decisions": {agent_id: "approve"}}

        if decision == "revise":
            # Loop back (router -> agent_id) to re-run the agent with this feedback.
            await emit(sid, {"type": "node_status", "node": agent_id, "status": "running",
                             "log": f"{agent_id} revise requested; re-running with feedback"})
            return {"decisions": {agent_id: "revise"}, "feedback": {agent_id: comment}}

        await emit(sid, {"type": "node_status", "node": agent_id, "status": "denied",
                         "log": f"{agent_id} denied; halting workflow"})
        return {"decisions": {agent_id: "deny"}, "status": {agent_id: "denied"}}

    return gate


def make_sequence_gate_node(gate_id: str, seq_ids: list[str], label: str):
    """A human-in-the-loop gate placed after a SEQUENTIAL group (the agents run
    one after another; the gate runs once, after the LAST one completes).

    Unlike a parallel group, there is a single decision for the whole sequence
    (approve / revise / deny) — the same UX as a single-agent gate. On "revise"
    the router loops back to the FIRST agent of the sequence so the whole chain
    re-runs with the reviewer's feedback (applied to every agent in the sequence).
    """

    async def gate(state: dict, config: RunnableConfig) -> dict:
        sid = config["configurable"]["thread_id"]
        if is_cancelled(sid):
            raise WorkflowCancelled(sid)
        # The last agent's output is the reviewable result of the sequence.
        out = (state.get("outputs") or {}).get(seq_ids[-1], state.get("topic", ""))
        question = f"Review {label}: {str(out)[:120]}"
        await emit(sid, {"type": "hitl_request", "node": gate_id, "question": question,
                         "log": f"Awaiting human review after {label}"})

        resumed = interrupt({"node": gate_id, "sequence": seq_ids, "question": question})
        if isinstance(resumed, dict):
            decision = resumed.get("decision", "approve")
            comment = resumed.get("comment", "") or ""
        else:
            decision, comment = (resumed or "approve"), ""

        await emit(sid, {"type": "hitl_resolved", "node": gate_id,
                         "log": f"{label} review: {decision}"})

        if decision == "revise":
            # Loop back to the first agent; the whole sequence re-runs. Feedback is
            # applied to every agent in the sequence so the relevant one sees it.
            for a in seq_ids:
                await emit(sid, {"type": "node_status", "node": a, "status": "running",
                                 "log": f"{label}: re-running {a}"})
            return {"decisions": {gate_id: "revise"},
                    "feedback": {a: comment for a in seq_ids}}

        if decision == "deny":
            for a in seq_ids:
                await emit(sid, {"type": "node_status", "node": a, "status": "denied",
                                 "log": f"{label} denied; halting workflow"})
            return {"decisions": {gate_id: "deny"}, "status": {a: "denied" for a in seq_ids}}

        return {"decisions": {gate_id: "approve"}}

    return gate


def make_group_gate_node(group_id: str, group_ids: list[str], label: str):
    """A human-in-the-loop gate placed after a PARALLEL group (all agents in the
    group must complete before it runs — LangGraph fan-in).

    The decision is recorded under a synthetic `group_id` (e.g. "research"), so
    routing is independent of any single agent. approve -> continue; deny ->
    halt; revise -> re-run EVERY agent in the group with the reviewer's
    feedback. The reviewer sees each agent's output in the workspace's outputs
    panel; this gate just carries the decision.
    """

    async def gate(state: dict, config: RunnableConfig) -> dict:
        sid = config["configurable"]["thread_id"]
        if is_cancelled(sid):
            raise WorkflowCancelled(sid)
        question = f"Review {label} — approve / revise / reject each agent (or all)."
        # Emit against the synthetic group_id; the sink handles a node id that is
        # not a per-agent status entry.
        await emit(sid, {"type": "hitl_request", "node": group_id, "question": question,
                         "log": f"Awaiting per-agent review after {label}"})

        resumed = interrupt({"node": group_id, "group": group_ids, "question": question})

        # Parse the reviewer's decision(s). Accepted shapes:
        #   {"decisions": {agent_id: {"decision": ..., "comment": ...}, ...}}  (per-agent)
        #   {"decision": "approve|revise|deny", "comment": ...}                 (bulk)
        #   "approve" | ...                                                     (bare string)
        # Per-agent decisions: approve (accept) | revise (re-run WITH feedback)
        #   | reject (re-run WITHOUT feedback) | deny (halt the whole workflow).
        per: dict = {}
        deny = False
        if isinstance(resumed, dict):
            per = resumed.get("decisions") or {}
            if not per and resumed.get("decision"):
                if resumed["decision"] == "deny":
                    deny = True
                else:
                    per = {a: {"decision": resumed["decision"],
                               "comment": resumed.get("comment", "")} for a in group_ids}
        elif isinstance(resumed, str):
            per = {a: {"decision": resumed} for a in group_ids}

        rerun: list[str] = []
        feedback: dict[str, str] = {}
        for a in group_ids:
            d = per.get(a) or {}
            dec = str(d.get("decision", "approve")).lower()
            if dec == "deny":
                deny = True
            elif dec in ("revise", "reject", "rerun"):
                rerun.append(a)
                # revise carries feedback; reject re-runs from scratch (clear any
                # stale feedback so the agent does not see a prior round's note).
                feedback[a] = d.get("comment", "") if dec == "revise" else ""

        summary = "deny" if deny else (f"re-run {','.join(rerun)}" if rerun else "approved")
        await emit(sid, {"type": "hitl_resolved", "node": group_id,
                         "log": f"{label} review: {summary}"})

        if deny:
            for a in group_ids:
                await emit(sid, {"type": "node_status", "node": a, "status": "denied",
                                 "log": f"{label} denied; halting workflow"})
            return {"decisions": {group_id: "deny"}, "status": {a: "denied" for a in group_ids}}

        if rerun:
            for a in rerun:
                await emit(sid, {"type": "node_status", "node": a, "status": "running",
                                 "log": f"{label}: re-running {a}"})
            return {"decisions": {group_id: "revise"}, "feedback": feedback,
                    "group_rerun": {group_id: rerun}}

        return {"decisions": {group_id: "approve"}}

    return gate
