"""Generic wrappers that turn agents and HITL gates into LangGraph nodes.

Agent authors never touch this — it handles status emission, output storage, the
interrupt/resume mechanics, and the config-driven AgentCore features (OTEL agent
span, token stamping, prompt capture for evaluations, long-term memory recall and
store) uniformly for every agent.
"""

from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt

from app.common import clock
from app.common.base import Agent
from app.common.config import AGENTS
from app.common.context import AgentContext
from app.common.sink import WorkflowCancelled, emit, is_cancelled
from app.features.guardrails import GuardrailBlocked
from app.features.observability import otel


def _eval_task_input(agent: Agent, ctx: AgentContext) -> str:
    """Fallback task description for AgentCore Evaluations, used only when the
    agent made no model call (so no real prompt was captured).

    Using the raw topic would make every agent look like it was asked for the
    whole deliverable, so specialist agents would score low on Correctness /
    InstructionFollowing. Instead we describe the agent's ACTUAL job (its role +
    the asset it must produce, from workflow.json) plus the concrete request, so
    each evaluator judges the agent against what it was really asked to do."""
    spec = AGENTS.get(agent.id) or {}
    produces = spec.get("produces") or "its deliverable"
    request = ctx.topic or agent.name
    parts = [
        f"You are the \"{agent.name}\" agent in a multi-agent workflow.",
        (f"Your task: produce the {produces} for this request, grounded in the "
        f"provided inputs and appropriate to your specialist role."),
        f"Request: {request}",
    ]
    if ctx.feedback:
        parts.append(f"Reviewer feedback to address: {ctx.feedback}")
    return "\n".join(parts)


def _guardrail_input(ctx: AgentContext) -> str:
    """The human-supplied text an INPUT guardrail should screen for this agent.

    Guardrails exist to catch untrusted input, and the untrusted parts of an
    agent's input are the free-text the human typed: the request itself and any
    reviewer feedback. Upstream agent outputs are screened by the OUTPUT guardrail
    of the agent that produced them, so they are not re-screened here."""
    parts = [ctx.topic or ""]
    if ctx.feedback:
        parts.append(ctx.feedback)
    return "\n\n".join(p for p in parts if p)


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
        # One span per agent so the CloudWatch Traces View shows each agent call.
        # We mark it as an AGENT span and carry the task input/output as the
        # gen_ai.task.* attributes AgentCore Evaluations reads to score the run
        # (the built-in evaluators need the prompt + response on the AGENT span).
        # This is what lets evaluation work WITHOUT re-enabling the LangChain
        # instrumentation (which is disabled for clean token metrics).
        # Holder for the agent's captured prompt/output (stamped below).
        prompt_holder: dict = {}
        with otel.span(f"agent.{agent.id}",
                       # AgentCore Evaluations only scores spans emitted under a
                       # recognised agent-framework instrumentation scope. We emit
                       # the agent span under the ADOT LangChain scope so the
                       # built-in evaluators accept it (the real LangChain
                       # instrumentation stays disabled for clean token metrics).
                       scope="amazon.opentelemetry.distro.instrumentation.langchain",
                       **{"gen_ai.agent.name": agent.name,
                          "gen_ai.agent.id": agent.id,
                          "gen_ai.operation.name": "invoke_agent",
                          "aws.genai.span_kind": "AGENT",
                          # Padded to match the runtimeSessionId form every other
                          # span uses, so the run is ONE session (not a dup pair).
                          "session.id": otel.normalize_session_id(ctx.session_id)}) as _agent_span:
            # Sum this agent's LLM-call tokens onto the agent span (real counts
            # from usage_metadata; the auto "chat" spans report 0 in ADOT 0.19.0).
            with otel.accumulate_tokens_on(_agent_span):
                # Capture the agent's REAL prompt (its model calls: role,
                # instructions, and the actual source inputs) so evaluators judge
                # substance, not a generic role paraphrase.
                _pcap = otel.begin_prompt_capture(prompt_holder)
                try:
                    # Guardrails + long-term memory are applied HERE, by the
                    # framework, so both are purely config-driven: flip them on
                    # for any agent in workflow.json and they take effect with no
                    # agent code. Each call is a no-op when disabled for the agent.
                    #
                    # INPUT guardrail: screen the human-supplied text before the
                    # agent runs. A block raises GuardrailBlocked (handled below).
                    await ctx.guardrail(_guardrail_input(ctx), "INPUT")
                    # Long-term memory: recall this agent's relevant past insights
                    # (ctx.llm auto-injects them into the system prompt), run, then
                    # store this run's output for future runs.
                    ctx.recalled_memory = await ctx.memory_recall(ctx.topic or agent.name)
                    out = await agent.run(ctx)
                    # OUTPUT guardrail: screen what the agent produced before it
                    # becomes an input to any downstream agent or the reviewer.
                    await ctx.guardrail(str(out), "OUTPUT")
                    await ctx.memory_store(out)
                except WorkflowCancelled:
                    raise  # user stop — reported as "cancelled", not a failure
                except GuardrailBlocked as _blocked:
                    # Content safety stopped this agent. Report it distinctly (not
                    # as a generic crash) with the guardrail's own message, then
                    # re-raise so the run halts.
                    await emit(ctx.session_id, {
                        "type": "node_status", "node": agent.id, "status": "failed",
                        "log": f"{agent.name} blocked by guardrail: {_blocked.message}"})
                    raise
                except Exception as _agent_err:
                    # Surface WHICH agent failed (and why) as this node's terminal
                    # status + a timeline line, so a failed run points straight at
                    # the culprit instead of a generic top-level error. Re-raise so
                    # the graph still halts and the run is marked failed.
                    await emit(ctx.session_id, {
                        "type": "node_status", "node": agent.id, "status": "failed",
                        "log": f"{agent.name} failed: {type(_agent_err).__name__}: {_agent_err}"})
                    raise
                finally:
                    otel.end_prompt_capture(_pcap)
            # gen_ai.task.input = the agent's ACTUAL prompt (instructions + real
            # source inputs) so AgentCore Evaluations scores substance (correct
            # extraction/reasoning, rule-following) rather than role/format
            # compliance. Falls back to a role descriptor if the agent made no
            # model call. task.output = the paired response from that SAME call,
            # so the evaluators see one coherent step (for agents that
            # post-process the model output into a different asset shape this
            # evaluates the substantive reasoning call rather than the mechanical
            # reshape, avoiding false "schema mismatch" penalties). Both truncated
            # to keep the span attributes a sane size.
            task_input = prompt_holder.get("prompt") or _eval_task_input(agent, ctx)
            task_output = prompt_holder.get("output") or str(out)
            otel.set_attrs(_agent_span, **{"gen_ai.task.input": str(task_input)[:40000],
                                           "gen_ai.task.output": str(task_output)[:12000]})

        # Append this run as the next version. The comment is the reviewer
        # feedback that caused a re-run (empty on the initial run).
        prior = list((state.get("history") or {}).get(agent.id, []))
        version = len(prior) + 1
        comment = (state.get("feedback") or {}).get(agent.id, "") or ""
        record = {"version": version, "at": clock.now_str(),
                  "comment": comment, "output": out}
        history = [*prior, record]

        await emit(ctx.session_id, {"type": "node_status", "node": agent.id,
                                    "status": "done", "output": out, "history": history,
                                    "log": f"{agent.name} complete (v{version})"})
        return {"status": {agent.id: "done"}, "outputs": {agent.id: out},
                "history": {agent.id: history}}

    return node


def _decision_of(resumed) -> tuple[str, str]:
    """(decision, comment) from an interrupt resume payload.

    Both resume paths build a dict — runtime.invoke and server.decide — so the shape is
    `{"decision", "comment", "decisions"}`. A bare string is still accepted because an
    interrupt can be resumed by any client of the graph, and defaulting is kinder than
    a TypeError deep inside a gate. This parse was copied into all three gate nodes.
    """
    if isinstance(resumed, dict):
        return str(resumed.get("decision", "approve")), str(resumed.get("comment") or "")
    return (str(resumed) if resumed else "approve"), ""


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

        resumed = interrupt({"node": agent_id, "question": question})  # pauses here
        decision, comment = _decision_of(resumed)

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
        decision, comment = _decision_of(resumed)

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
    halt; revise -> re-run the flagged agents with the reviewer's feedback. The
    reviewer sees each agent's output in the workspace's outputs panel; this gate
    just carries the decision.
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
