"""Builds the LangGraph StateGraph from workflow.json.

Each item in "steps" is one of:
  * a single agent            {"agent": "intake"}
  * a "parallel" group        {"parallel": ["knowledge_research", "web_search"]}
  * a "sequence" group        {"sequence": ["analysis", "recommendation"]}
Steps run in order. A parallel group runs its agents concurrently and joins; a
sequence group runs its agents one after another. Any step may carry "hitl": true
for a human-approval gate after it. HITL gates use conditional routing:
approve -> continue, deny -> END, revise -> loop back and re-run with feedback
(a single agent / the whole sequence, or the flagged subset of a parallel group).

    step: {"agent": "intake", "hitl": true}                       -> agent, then a human gate
    step: {"parallel": ["knowledge_research", "web_search"]}     -> both concurrently, join after
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


# --- Rewind / "rerun from an agent" planning -------------------------------
#
# Given a target agent, compute how to rewind the graph so that agent (and
# everything downstream) re-runs, WITHOUT restarting the whole workflow.
#
# The mechanism (see runtime._rewind / server._rewind):
#   1. graph.aupdate_state(cfg, values, as_node=<predecessor>) — attributes a
#      state write to the node that routes INTO the target, which makes the
#      graph's pending tasks become the target entry node(s).
#   2. graph.ainvoke(None, cfg) — runs those pending tasks and then cascades
#      forward through every downstream agent and gate. Gate nodes always call
#      interrupt(), so each downstream gate naturally RE-PAUSES for human review.
#
# `values` seeds the predecessor gate's routing decision as "approve" (so the
# router forwards to the target rather than looping/halting) and injects the
# reviewer's feedback into the target agent.


def _step_index_of(agent_id: str) -> int:
    for i, step in enumerate(STEPS):
        if agent_id in _agents_in(step):
            return i
    return -1


def _predecessor_of_step(si: int) -> tuple[str, str | None]:
    """(as_node, decision_key) for the node that routes INTO step `si`.

    A gated step is entered from its gate (whose router must be told "approve");
    an ungated one is entered straight from the single node before it.
    """
    if si == 0:
        return START, None
    prev, pi = STEPS[si - 1], si - 1
    if prev.get("hitl"):
        if "agent" in prev:
            return f"{prev['agent']}_gate", prev["agent"]
        gid = _gate_id(prev, pi)
        return f"{gid}_gate", gid
    # Ungated predecessor: a sequence ends at its last agent; a single agent is
    # itself. A non-gated parallel group has no single predecessor node, so a
    # rewind across it would be ambiguous — reject it rather than guess.
    if "sequence" in prev:
        return prev["sequence"][-1], None
    prev_agents = _agents_in(prev)
    if len(prev_agents) != 1:
        raise ValueError(
            "cannot rewind across the previous step: it is a non-gated parallel "
            "group with no single predecessor node")
    return prev_agents[0], None


def rerun_plan(agent_id: str) -> dict:
    """Plan a rewind so `agent_id` re-runs and cascades downstream.

    Returns:
      as_node          - node to attribute the state write to (its successors
                         become the next tasks): START for the first step, the
                         previous step's gate, the node before it in a sequence,
                         or the single ungated predecessor.
      decision_key     - decision to force to "approve" so the predecessor gate's
                         router forwards here (None when there is no gate).
      step_agents      - the agents that will re-run in the target step. A
                         parallel group re-runs as a unit; a sequence re-runs from
                         the target agent onward.
      downstream_agents- step_agents + all agents after the target step (for UI reset).
      step_index       - index of the target step.

    Raises ValueError for an unknown agent, or a rewind that would cross a
    non-gated parallel step (ambiguous single predecessor).
    """
    si = _step_index_of(agent_id)
    if si < 0:
        raise ValueError(f"unknown agent '{agent_id}'")

    step = STEPS[si]
    later_agents: list[str] = []
    for s in STEPS[si + 1:]:
        later_agents += _agents_in(s)

    seq = step.get("sequence")
    if seq and agent_id != seq[0]:
        # Mid-sequence target: enter from the agent immediately before it, and
        # re-run the tail of the chain from there.
        k = seq.index(agent_id)
        step_agents = seq[k:]
        return {"as_node": seq[k - 1], "decision_key": None,
                "step_agents": step_agents,
                "downstream_agents": step_agents + later_agents,
                "step_index": si}

    # The target is the step's entry: a single agent, any member of a parallel
    # group (the stage re-runs as a unit), or the first agent of a sequence.
    step_agents = _agents_in(step)
    as_node, decision_key = _predecessor_of_step(si)
    return {"as_node": as_node, "decision_key": decision_key,
            "step_agents": step_agents,
            "downstream_agents": step_agents + later_agents,
            "step_index": si}


def group_rerun_plan(agent_ids: list[str]) -> dict:
    """Plan a rewind that re-runs a SUBSET of a gated parallel stage.

    Used for the post-completion "re-run these agents" flow: the user picks 2+
    agents from one parallel stage, only those re-run in parallel, then the
    stage's review gate re-pauses so the refreshed outputs can be reviewed before
    the workflow cascades downstream.

    Mechanism: this reuses the group gate's existing subset routing. We seed the
    gate's decision to "revise" with group_rerun=<subset>, so the group router
    (_make_group_router above) loops back to exactly that subset. The rewind
    therefore attributes its state write to the group's OWN gate node.

    Returns:
      as_node          - the group gate node (its router forwards to the subset).
      group_id         - the synthetic gate/group id (e.g. "research").
      subset           - the selected agents to re-run (validated as a subset).
      downstream_agents- subset + every agent in later steps (for UI reset). The
                         non-selected siblings in the SAME stage keep their output.
      step_index       - index of the target parallel stage.

    Raises ValueError if the agents are empty, span more than one step, or the
    target step is not a gated parallel group.
    """
    ids = [a for a in dict.fromkeys(agent_ids or []) if a]  # de-dup, keep order
    if not ids:
        raise ValueError("no agents selected to re-run")

    indices = {_step_index_of(a) for a in ids}
    if -1 in indices:
        bad = [a for a in ids if _step_index_of(a) < 0]
        raise ValueError(f"unknown agent(s): {', '.join(bad)}")
    if len(indices) != 1:
        raise ValueError("selected agents must all belong to the same stage")

    si = indices.pop()
    step = STEPS[si]
    if "parallel" not in step or not step.get("hitl"):
        raise ValueError("multi-agent re-run requires a gated parallel stage")

    group_id = _gate_id(step, si)
    downstream_agents: list[str] = list(ids)
    for s in STEPS[si + 1:]:
        downstream_agents += _agents_in(s)

    return {"as_node": f"{group_id}_gate", "group_id": group_id,
            "subset": ids, "downstream_agents": downstream_agents,
            "step_index": si}
