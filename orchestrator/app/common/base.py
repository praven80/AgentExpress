"""Base class for agents.

Every agent is a package under app/subagents/<name>/ (agent.py with an Agent
subclass implementing run(), plus __init__.py doing `from .agent import agent`),
listed under "agents" in workflow.json and placed in "steps".

Its `runtime` decides WHERE it runs, not what it does:
  * "main" (default) — runs in-process as a node in the orchestrator runtime.
  * "dedicated"      — runs in its OWN AgentCore Runtime (provisioned by Terraform
                       from workflow.json); the orchestrator invokes it via
                       InvokeAgentRuntime. Same run() code either way.

Its `agentcore` block declares which AgentCore features it uses (memory,
guardrails, evaluations, policy, …). The framework applies them around run()
based on that config, so enabling a capability for an agent is a workflow.json
edit — not a code change. See app/features/ for what each one does.

The framework wires either mode into the graph, status tracking, and UI.
"""

from app.common.context import AgentContext


class Agent:
    # Populated by the registry from workflow.json:
    id: str = ""
    name: str = ""
    kind: str = "sync"          # "sync" or "async" (long-running); metadata for the UI
    runtime: str = "main"       # "main" (in-process node) or "dedicated" (own AgentCore Runtime)
    tool: str | None = None     # optional tool label from workflow.json `tools`
    corpus: str | None = None   # for a type=kb tool: which corpus to retrieve from
    model: str | None = None    # optional per-agent model id (defaults to config.MODEL_ID)
    temperature: float = 0
    # Output budget in tokens, from `maxTokens` in this agent's workflow.json entry.
    # Set it there, not here: an agent emitting a large structured payload needs a
    # bigger budget, and too small a budget truncates the JSON mid-object.
    max_tokens: int = 4000
    agentcore: dict = None      # AgentCore features config (workflow.json "agentcore" block)

    # Overridden by subclasses:
    system_prompt: str = ""

    async def run(self, ctx: AgentContext) -> str:
        """Do the agent's work and return its output text.

        Use ctx.input('X') for upstream outputs, ctx.llm(...) for the model,
        ctx.call_tool(...)/ctx.retrieve(...) for tools, and ctx.heartbeat(pct)/ctx.log(msg)
        for progress. The AgentCore feature helpers (ctx.guardrail,
        ctx.memory_recall, ctx.get_identity_token, ctx.policy_check) are safe to
        call unconditionally — each is a no-op when disabled in workflow.json.
        """
        raise NotImplementedError
