"""Base class for agents.

Every agent is a package under app/subagents/<name>/ (agent.py with an Agent
subclass implementing run(), plus __init__.py doing `from .agent import agent`),
listed under "agents" in workflow.json and placed in "steps".

Its `runtime` decides WHERE it runs, not what it does:
  * "main" (default) — runs in-process as a node in the orchestrator runtime.
  * "dedicated"      — runs in its OWN AgentCore Runtime (provisioned by Terraform
                       from workflow.json); the orchestrator invokes it via
                       InvokeAgentRuntime. Same run() code either way.

The framework wires either mode into the graph, status tracking, and UI.
"""

from app.common.context import AgentContext


class Agent:
    # Populated by the registry from workflow.json:
    id: str = ""
    name: str = ""
    kind: str = "sync"          # "sync" or "async" (long-running); metadata for the UI
    runtime: str = "main"       # "main" (in-process node) or "dedicated" (own AgentCore Runtime)
    mcp: str | None = None      # optional MCP server key (reached via the Gateway)
    model: str | None = None    # optional per-agent model id (defaults to config.MODEL_ID)
    temperature: float = 0
    max_tokens: int = 300

    # Overridden by subclasses:
    system_prompt: str = ""

    async def run(self, ctx: AgentContext) -> str:
        """Do the agent's work and return its output text.

        Use ctx.input('X') for upstream outputs, ctx.llm(...) for the model,
        ctx.mcp(...) for tools, and ctx.heartbeat(pct)/ctx.log(msg) for progress.
        """
        raise NotImplementedError
