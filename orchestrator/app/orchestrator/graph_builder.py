"""Builds the LangGraph StateGraph from workflow.json.

Each item in "steps" is one of:
  * a single agent            {"agent": "intake"}
  * a "parallel" group        {"parallel": ["knowledge_research", "web_research"]}
  * a "sequence" group        {"sequence": ["analysis", "recommendation"]}
Steps run in order. A parallel group runs its agents concurrently and joins; a
sequence group runs its agents one after another. Any step may carry "hitl": true
for a human-approval gate after it. HITL gates use conditional routing:
approve -> continue, deny -> END, revise -> loop back and re-run with feedback
(a single agent / the whole sequence, or the flagged subset of a parallel group).

    step: {"agent": "intake", "hitl": true}                       -> agent, then a human gate
    step: {"parallel": ["knowledge_research", "web_research"]}     -> both concurrently, join after
    step: {"sequence": ["analysis", "recommendation"]}            -> one after another, one gate after both
"""

from langgraph.graph import END, START, StateGraph

from app.common.config import STEPS
from app.orchestrator.nodes import (
    make_agent_node,
    make_gate_node,
    make_group_gate_node,
    make_sequence_gate_node,
)
from app.orchestrator.registry import load_agents
from app.common.state import State


def _agents_in(step: dict) -> list:
    """Every agent in a step (parallel = all, sequence = all in order)."""
    return step.get("parallel") or step.get("sequence") or [step["agent"]]


def _entries(step: dict) -> list:
    """Node(s) where incoming edges land. A sequence begins at its FIRST agent;
    a parallel group begins at all of its agents; a single step at its agent."""
    if "sequence" in step:
        return [step["sequence"][0]]
    return step.get("parallel") or [step["agent"]]


def _gate_id(step: dict, i: int) -> str:
    """Synthetic decision/gate key for a gated group."""
    return step.get("gateId") or f"group{i}"


def _make_router(agent_id: str, next_entries: list):
    def router(state: dict):
        decision = (state.get("decisions") or {}).get(agent_id)
        if decision == "deny":
            return END
        if decision == "revise":
            return agent_id  # loop back: re-run the agent with the feedback
        return END if next_entries == [END] else next_entries
    return router


def _make_group_router(group_id: str, group_ids: list, next_entries: list):
    def router(state: dict):
        decision = (state.get("decisions") or {}).get(group_id)
        if decision == "deny":
            return END
        if decision == "revise":
            # Loop back to ONLY the agents the reviewer marked revise/reject;
            # the gate re-runs after that subset completes.
            rerun = (state.get("group_rerun") or {}).get(group_id)
            return rerun or group_ids
        return END if next_entries == [END] else next_entries
    return router


def _make_sequence_router(gate_id: str, first_id: str, next_entries: list):
    def router(state: dict):
        decision = (state.get("decisions") or {}).get(gate_id)
        if decision == "deny":
            return END
        if decision == "revise":
            return first_id  # loop back to the start of the sequence
        return END if next_entries == [END] else next_entries
    return router


def build_graph(checkpointer=None):
    if checkpointer is None:
        from langgraph.checkpoint.memory import MemorySaver
        checkpointer = MemorySaver()

    registry = load_agents()
    g = StateGraph(State)

    # Agent nodes.
    for agent_id, agent in registry.items():
        g.add_node(agent_id, make_agent_node(agent))

    # Gate nodes. A single-agent gate loops back to the one agent on "revise";
    # a parallel-group gate re-runs the flagged agents; a sequence gate re-runs
    # the whole chain from its first agent.
    gate_of: dict[int, str] = {}
    for i, step in enumerate(STEPS):
        if not step.get("hitl"):
            continue
        if "agent" in step:
            aid = step["agent"]
            gname = f"{aid}_gate"
            g.add_node(gname, make_gate_node(aid, registry[aid].name))
        elif "parallel" in step:
            gid = _gate_id(step, i)
            gname = f"{gid}_gate"
            g.add_node(gname, make_group_gate_node(gid, step["parallel"],
                                                   step.get("gateName") or gid))
        else:  # sequence
            gid = _gate_id(step, i)
            gname = f"{gid}_gate"
            g.add_node(gname, make_sequence_gate_node(gid, step["sequence"],
                                                      step.get("gateName") or gid))
        gate_of[i] = gname

    # Chain the agents inside every sequence group (agent[k] -> agent[k+1]).
    for step in STEPS:
        seq = step.get("sequence")
        if seq:
            for a, b in zip(seq, seq[1:]):
                g.add_edge(a, b)

    # START -> first step's entry node(s).
    for entry in _entries(STEPS[0]):
        g.add_edge(START, entry)

    # Wire each step to the next.
    for i, step in enumerate(STEPS):
        is_last = i == len(STEPS) - 1
        next_entries = [END] if is_last else _entries(STEPS[i + 1])
        hitl = step.get("hitl")

        if "agent" in step and hitl:
            aid = step["agent"]
            gname = gate_of[i]
            g.add_edge(aid, gname)  # agent -> its gate
            # Targets include `aid` so a "revise" decision can loop back to it.
            g.add_conditional_edges(gname, _make_router(aid, next_entries),
                                    list({*next_entries, END, aid}))
        elif "parallel" in step and hitl:
            # Parallel group with a join gate: every agent -> gate; revise
            # re-runs the flagged agents.
            gid = _gate_id(step, i)
            gname = gate_of[i]
            group_ids = step["parallel"]
            for a in group_ids:
                g.add_edge(a, gname)
            g.add_conditional_edges(gname, _make_group_router(gid, group_ids, next_entries),
                                    list({*next_entries, END, *group_ids}))
        elif "sequence" in step and hitl:
            # Sequence group with one gate after the LAST agent; revise loops
            # back to the FIRST agent (the whole chain re-runs).
            gid = _gate_id(step, i)
            gname = gate_of[i]
            seq = step["sequence"]
            g.add_edge(seq[-1], gname)
            g.add_conditional_edges(gname, _make_sequence_router(gid, seq[0], next_entries),
                                    list({*next_entries, END, seq[0]}))
        elif "sequence" in step:
            # Ungated sequence: last agent flows into the next step.
            for ne in next_entries:
                g.add_edge(step["sequence"][-1], ne)
        else:
            # Ungated single agent or parallel group: each agent flows into the
            # next step's entries.
            for a in _agents_in(step):
                for ne in next_entries:
                    g.add_edge(a, ne)

    return g.compile(checkpointer=checkpointer)
