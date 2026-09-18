"""Agent registry: builds the map of agent_id -> Agent instance from workflow.json.

An agent's `runtime` decides its implementation:
  * runtime "main"      -> import app.subagents.<module> and run it in-process
                           (a LangGraph node inside the orchestrator runtime).
  * runtime "dedicated" -> AgentCoreRuntimeAgent: the orchestrator invokes that
                           agent's OWN AgentCore Runtime (its own container),
                           passing/returning the same data an in-process agent
                           would. The dedicated runtime itself runs the real
                           module via build_agent_module() below.
Both expose the same Agent interface, so the graph wiring is identical.
"""

import importlib

from app.common.agentcore_agent import AgentCoreRuntimeAgent
from app.common.base import Agent
from app.common.config import AGENTS, output_rules_for


def _configure(agent: Agent, agent_id: str, spec: dict) -> Agent:
    """Copy the workflow.json spec onto an Agent instance."""
    agent.id = agent_id
    agent.name = spec.get("name", agent_id)
    agent.kind = spec.get("kind", "sync")
    agent.runtime = spec.get("runtime", "main")
    # The tool this agent is bound to (a key in the workflow.json `tools`
    # block), and for a Knowledge Base tool the corpus it is scoped to.
    agent.tool = spec.get("tool")
    agent.corpus = spec.get("corpus")
    agent.model = spec.get("model")  # None -> config.MODEL_ID default
    agent.temperature = spec.get("temperature", 0)
    # Output budget for this agent's model calls, in tokens. camelCase to match the
    # rest of workflow.json (the old snake_case `max_tokens` key was never set by
    # any config, so every agent silently used the 300 default and then overrode it
    # with a literal at the call site — which put the budget in code, not config).
    #
    # It matters per agent: a research agent emits a full structured JSON payload
    # with several classified findings, and a budget that is too small truncates it
    # MID-JSON, which surfaces as "the model returned no parseable research JSON"
    # rather than as an obvious limit problem.
    agent.max_tokens = int(spec.get("maxTokens") or 4000)
    # Output-rule enforcement for this agent, and whether a failed check may spend
    # a second model call repairing itself. Engine default + per-agent override;
    # see config.output_rules_for. It lives on the Agent (and so on ctx) because
    # `repair` is a COST decision, and cost is per agent: a customer may well want
    # the report re-asked and not the four research agents.
    agent.output_rules = output_rules_for(spec)
    # AgentCore feature flags (memory, guardrails, evaluations, policy, …).
    # AgentContext reads these to decide which features apply to this agent.
    agent.agentcore = dict(spec.get("agentcore") or {})
    return agent


def build_agent_module(agent_id: str) -> Agent:
    """Import and configure an agent's REAL in-process implementation, ignoring
    its runtime placement. Used by the per-agent runtime (subagent_runtime) to
    run a dedicated agent's actual code inside its own container.

    The agent id IS the subagent package name — one convention, no override. An
    agent key in workflow.json maps to app/subagents/<that key>/, so adding an
    agent is a config entry plus a folder, and nothing has to name it twice."""
    spec = AGENTS[agent_id]
    module = importlib.import_module(f"app.subagents.{agent_id}")
    return _configure(module.agent, agent_id, spec)


def load_agents() -> dict[str, Agent]:
    """The orchestrator's view: dedicated agents are invoked cross-runtime."""
    registry: dict[str, Agent] = {}
    for agent_id, spec in AGENTS.items():
        if spec.get("runtime", "main") == "dedicated":
            agent: Agent = AgentCoreRuntimeAgent()
            _configure(agent, agent_id, spec)
        else:
            agent = build_agent_module(agent_id)
        registry[agent_id] = agent
    return registry
