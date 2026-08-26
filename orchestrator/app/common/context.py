"""AgentContext — the single object handed to every agent's run() method.

It hides the plumbing (state shape, event emission, LLM/MCP clients) so an
agent author only writes business logic. It also carries the agent's configured
model so ctx.llm() uses the right one per agent.
"""

from app.common.llm import run_llm
from app.common.mcp import query_mcp
from app.common.mcp import retrieve as retrieve_kb
from app.common.sink import WorkflowCancelled, emit, is_cancelled


class AgentContext:
    def __init__(self, agent, state: dict, config: dict):
        self.agent_id = agent.id
        self.model = agent.model
        self.temperature = agent.temperature
        self.max_tokens = agent.max_tokens
        self.state = state
        self.session_id = config["configurable"]["thread_id"]
        self.topic = state.get("topic", "")
        self.user = state.get("user", "")
        # Tag every LLM/tool call made during this agent's run with who/what/which
        # session, for the observability metering (isolated in app/observability).
        try:
            from app.observability.scope import set_scope
            set_scope(self.session_id, self.agent_id, self.user)
        except Exception:  # noqa: BLE001 - observability is best-effort
            pass
        # Reviewer feedback for this agent, set when a HITL gate chose "revise"
        # and the graph looped back to re-run this agent. Empty on a first run.
        self.feedback = (state.get("feedback") or {}).get(agent.id, "")

    def input(self, agent_id: str):
        """Return the output produced by an upstream agent, or None."""
        return (self.state.get("outputs") or {}).get(agent_id)

    async def llm(self, system: str, user: str, model: str | None = None,
                  max_tokens: int | None = None) -> str:
        """Call the model (this agent's configured model, with simulated fallback).

        `max_tokens` overrides the agent's configured budget for this one call —
        used when a step must emit a large structured payload (e.g. a full
        research JSON) that would otherwise be truncated."""
        return await run_llm(self.agent_id, system, user,
                             model=model or self.model,
                             temperature=self.temperature,
                             max_tokens=max_tokens or self.max_tokens)

    async def mcp(self, server: str, query: str):
        """Call an MCP server; returns (summary, mode)."""
        return await query_mcp(server, query)

    async def retrieve(self, query: str, doc_type: str | None = None):
        """Retrieve grounded context from the KB via the Gateway; returns (text, mode).

        Pass `doc_type` to scope retrieval to a single corpus (e.g. "benchmark")
        so the agent only sees chunks tagged with that doc_type metadata."""
        return await retrieve_kb(query, doc_type=doc_type)

    async def heartbeat(self, pct: int) -> None:
        """Report progress for a long-running agent (updates the UI bar).

        Also the cancellation checkpoint for long agents: if the user hit Stop,
        raise so the run aborts mid-flight instead of finishing the work."""
        if is_cancelled(self.session_id):
            raise WorkflowCancelled(self.session_id)
        await emit(self.session_id, {"type": "heartbeat", "node": self.agent_id, "pct": pct})

    async def log(self, msg: str) -> None:
        """Append a line to the session timeline."""
        await emit(self.session_id, {"type": "log", "node": self.agent_id, "log": msg})
