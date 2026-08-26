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
from app.common.config import AGENTS


def _configure(agent: Agent, agent_id: str, spec: dict) -> Agent:
    """Copy the workflow.json spec onto an Agent instance."""
    agent.id = agent_id
    agent.name = spec.get("name", agent_id)
    agent.kind = spec.get("kind", "sync")
    agent.runtime = spec.get("runtime", "main")
    agent.mcp = spec.get("mcp")
    agent.model = spec.get("model")  # None -> config.MODEL_ID default
    agent.temperature = spec.get("temperature", 0)
    agent.max_tokens = spec.get("max_tokens", 300)
    return agent


def build_agent_module(agent_id: str) -> Agent:
    """Import and configure an agent's REAL in-process implementation, ignoring
    its runtime placement. Used by the per-agent runtime (subagent_runtime) to
    run a dedicated agent's actual code inside its own container.

    The agent id IS the subagent package name by convention; `module` in the
    spec is an optional override for the rare case they differ."""
    spec = AGENTS[agent_id]
    module = importlib.import_module(f"app.subagents.{spec.get('module', agent_id)}")
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
