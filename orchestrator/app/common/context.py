"""AgentContext — the single object handed to every agent's run() method.

It hides the plumbing (state shape, event emission, LLM/MCP clients, AgentCore
features) so an agent author only writes business logic. It also carries the
agent's configured model so ctx.llm() uses the right one per agent.

The AgentCore feature methods at the bottom are all CONFIG-DRIVEN: each reads the
agent's `agentcore` block from workflow.json and is a no-op when the feature is
disabled. An agent can therefore call them unconditionally.
"""

import re

from app.common.llm import run_llm
from app.common.sink import WorkflowCancelled, emit, is_cancelled
from app.features.gateway.client import query_tool
from app.features.gateway.client import retrieve as retrieve_kb


def _slug(text: str) -> str:
    """Lowercase, hyphenated, alphanumeric-only slug (safe for a memory actorId)."""
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")


class AgentContext:
    def __init__(self, agent, state: dict, config: dict):
        self.agent_id = agent.id
        self.model = agent.model
        self.temperature = agent.temperature
        self.max_tokens = agent.max_tokens
        # The tool this agent is bound to (a key in the workflow.json `tools`
        # block) and, for a Knowledge Base tool, the corpus it may retrieve from.
        # Both come straight from config, so an agent's data source is declared
        # rather than coded.
        self.tool = getattr(agent, "tool", None)
        self.corpus = getattr(agent, "corpus", None)
        self.state = state
        self.session_id = config["configurable"]["thread_id"]
        self.topic = state.get("topic", "")
        self.user = state.get("user", "")
        # Optional grouping key threaded from the invoke payload -> initial graph
        # state. It scopes long-term memory so an agent recalls insights for THIS
        # subject only (use it for whatever your domain groups knowledge by — a
        # customer, product, project, brand). Empty = scoped per agent only.
        self.subject_id = state.get("subject_id", "")
        # Long-term insights recalled for this agent (populated by the node
        # wrapper before run() when memory.longTerm is enabled in workflow.json).
        # ctx.llm() auto-injects these into the system prompt, so ANY agent
        # benefits from memory with zero agent code — purely config-driven.
        self.recalled_memory: list[str] = []
        # AgentCore features config (from the workflow.json "agentcore" block).
        self._agentcore = dict(getattr(agent, "agentcore", None) or {})
        # Tag every LLM/tool call made during this agent's run with who/what/which
        # session, for the observability metering (isolated in app/features/observability).
        # Version of THIS agent run = prior completed runs + 1 (matches the
        # numbering the node writes to `history`), so the observability UI can
        # group and label telemetry by version across re-runs.
        self.version = len((state.get("history") or {}).get(agent.id) or []) + 1
        try:
            from app.features.observability.scope import set_scope
            set_scope(self.session_id, self.agent_id, self.user, self.version)
        except Exception:  # noqa: BLE001 - observability is best-effort
            pass
        # Reviewer feedback for this agent, set when a HITL gate chose "revise"
        # and the graph looped back to re-run this agent. Empty on a first run.
        self.feedback = (state.get("feedback") or {}).get(agent.id, "")

    def input(self, agent_id: str):
        """Return the output produced by an upstream agent, or None."""
        return (self.state.get("outputs") or {}).get(agent_id)

    async def llm(self, system: str, user: str, model: str | None = None,
                  max_tokens: int | None = None, name: str | None = None) -> str:
        """Call the model (this agent's configured model). Raises ModelUnavailable
        if the Bedrock call fails — there is no placeholder response.

        `max_tokens` overrides the agent's configured budget for this one call —
        used when a step must emit a large structured payload (e.g. a full
        research JSON) that would otherwise be truncated.

        `name` names THIS model call (defaults to the agent id). An agent that
        issues several distinct prompts should name each one, so observability and
        AgentCore Evaluations can scope to a single prompt.

        If long-term memory was recalled for this agent (memory enabled in
        workflow.json), those past insights are auto-injected into the system
        prompt here — so memory influences the model with no per-agent code."""
        if self.recalled_memory:
            system = system + (
                "\n\n=== RELEVANT PAST INSIGHTS (long-term memory) ===\n"
                + "\n---\n".join(self.recalled_memory)
                + "\n(Use only as background context; prefer the current inputs "
                  "and never present a recalled figure as a new sourced fact.)"
            )
        return await run_llm(name or self.agent_id, system, user,
                             model=model or self.model,
                             temperature=self.temperature,
                             max_tokens=max_tokens or self.max_tokens)

    async def call_tool(self, tool_key: str, query: str):
        """Call a Gateway tool by its `tool` label in workflow.json.

        Named call_tool, not tool, because `ctx.tool` is the ATTRIBUTE holding
        this agent's declared tool label (see __init__).

        Works for every declared tool type — Knowledge Base, the managed Web
        Search connector, a remote MCP server, or an OpenAPI target — because the
        argument shape comes from the tool's declared type, not from the caller.
        Returns (summary, "gateway"). Raises ToolUnavailable or ToolDenied if the
        tool could not be called — the framework never invents evidence.
        """
        return await query_tool(tool_key, query)

    async def retrieve(self, query: str, doc_type: str | None = None):
        """Retrieve grounded context from the KB via the Gateway; returns (text, mode).

        Pass `doc_type` to scope retrieval to a single corpus (e.g. "reference")
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

    # --- AgentCore feature methods -----------------------------------------
    # Each method is a no-op if the feature is disabled in workflow.json.
    # Agents call these unconditionally; the config decides what runs.

    async def guardrail(self, text: str, source: str = "INPUT") -> str:
        """Check text against Bedrock Guardrails. No-op if disabled for this
        source in workflow.json. source: "INPUT" (before LLM) or "OUTPUT" (after).

        The guardrail is chosen by config: the agent's optional
        guardrails.guardrailId in workflow.json, else the GUARDRAIL_ID env
        default provisioned by Terraform. Every check (pass or block) is recorded
        for the observability inspector; a block raises GuardrailBlocked."""
        cfg = self._agentcore.get("guardrails", {})
        if not cfg.get(source.lower()):
            return text
        import time as _t

        from app.features.guardrails.client import GuardrailBlocked, check
        gid = cfg.get("guardrailId", "")
        start = _t.perf_counter()
        try:
            result = await check(text, source, guardrail_id=gid)
        except GuardrailBlocked as e:
            self._record_guardrail(source, "blocked", e.message,
                                   int((_t.perf_counter() - start) * 1000))
            raise
        self._record_guardrail(source, "passed", "(allowed)",
                               int((_t.perf_counter() - start) * 1000))
        return result

    def _record_guardrail(self, source: str, action: str, detail: str, latency_ms: int) -> None:
        """Best-effort: record a guardrail check for the observability UI."""
        try:
            from app.features.observability import meter
            meter.record_guardrail(source=source, action=action, detail=detail,
                                   latency_ms=latency_ms)
        except Exception:  # noqa: BLE001
            pass

    def _memory_actor(self) -> str:
        """Long-term memory actor id: the agent, scoped by subject when one is set
        (e.g. "analysis-acme"). The semantic strategy's namespace template
        "insights/{actorId}" turns this into the per-agent, per-subject namespace
        "insights/analysis-acme"."""
        subject = _slug(self.subject_id)
        return f"{self.agent_id}-{subject}" if subject else self.agent_id

    def _record_memory(self, op: str, namespace: str, query: str, result_text: str,
                       latency_ms: int) -> None:
        """Best-effort: record a memory read/write for the observability UI."""
        try:
            from app.features.observability import meter
            meter.record_memory(op=op, namespace=namespace, query=query,
                                result_text=result_text, latency_ms=latency_ms)
        except Exception:  # noqa: BLE001
            pass

    # Long-term strategy -> namespace prefix. Matches the strategy namespace
    # templates provisioned on the semantic memory (terraform/main.tf):
    #   semantic -> insights/{actorId}   summary -> summary/{actorId}/{sessionId}
    # Only the strategies both IaC paths actually provision (terraform/main.tf
    # semantic_memory_strategy + summary_memory_strategy, and the CDK equivalents).
    # `user_preference` was listed here without a provisioned strategy, so enabling
    # it recalled from a namespace nothing ever writes — and reported success.
    _NS_PREFIX = {"semantic": "insights", "summary": "summary"}

    def _longterm_strategies(self) -> list[str]:
        """The long-term strategies enabled for this agent in workflow.json.
        Accepts a list (["semantic","summary"]) or a bare truthy (-> semantic)."""
        lt = self._agentcore.get("memory", {}).get("longTerm")
        if not lt:
            return []
        return lt if isinstance(lt, list) else ["semantic"]

    def _namespace_for(self, strat: str) -> str:
        """Concrete namespace for a strategy, matching the templates provisioned
        in terraform/main.tf. Semantic is actor-scoped (cross-session); summary is
        session-scoped (AgentCore requires {sessionId} for summarization)."""
        actor = self._memory_actor()
        if strat == "summary":
            return f"summary/{actor}/{self.session_id}"
        return f"{self._NS_PREFIX.get(strat, strat)}/{actor}"

    async def memory_recall(self, query: str) -> list[str]:
        """Recall past insights from long-term memory (scoped to this agent +
        subject). Searches EVERY configured strategy's namespace and combines
        them. Each recall is recorded for the observability inspector. Returns []
        if memory is disabled for this agent."""
        strategies = self._longterm_strategies()
        if not strategies:
            return []
        import time as _t

        from app.features.memory import recall
        combined: list[str] = []
        for strat in strategies:
            namespace = self._namespace_for(strat)
            start = _t.perf_counter()
            try:
                results = await recall(query, namespace=namespace)
            except Exception as e:  # noqa: BLE001 - memory must never break a run
                self._record_memory("recall", namespace, query,
                                    f"(recall error: {type(e).__name__})",
                                    int((_t.perf_counter() - start) * 1000))
                continue
            self._record_memory(
                "recall", namespace, query,
                "\n---\n".join(str(x) for x in results) if results else "(no matching memories)",
                int((_t.perf_counter() - start) * 1000))
            combined.extend(results)
        return combined

    async def memory_store(self, content: str) -> None:
        """Store an insight in long-term memory (scoped to this agent + subject)
        for future sessions. One stored turn feeds ALL enabled strategies (each
        extracts into its own namespace). No-op if disabled. Recorded for the UI."""
        strategies = self._longterm_strategies()
        if not strategies:
            return
        import time as _t

        from app.features.memory import store
        actor = self._memory_actor()
        start = _t.perf_counter()
        # store() writes ONE short-term event under this actor; every matching
        # strategy (semantic, summary, …) then extracts from it asynchronously.
        targets = ", ".join(self._namespace_for(s) for s in strategies)
        try:
            await store(self.session_id, actor, content)
        except Exception as e:  # noqa: BLE001 - memory must never break a run
            self._record_memory("store", targets, "", f"(store error: {type(e).__name__})",
                                int((_t.perf_counter() - start) * 1000))
            return
        self._record_memory("store", targets, "", content,
                            int((_t.perf_counter() - start) * 1000))

    async def get_identity_token(self, provider: str = "") -> str:
        """Get an OAuth token via AgentCore Workload Identity, for calling an
        external API directly. Returns '' if identity is not configured for this
        agent (so the caller degrades to no token rather than failing)."""
        cfg = self._agentcore.get("identity", {})
        outbound = cfg.get("outbound", [])
        target = provider or (outbound[0] if outbound else "")
        if not target:
            return ""
        from app.features.identity import get_token
        return await get_token(target)

    async def policy_check(self, action: str) -> bool:
        """Optional app-level Cedar authorization check. Returns True when policy
        is disabled for this agent or the engine allows the action.

        NOTE: this is the SECONDARY path. Tool calls made through the Gateway are
        already governed server-side by the attached policy engine — see
        app/features/policy/."""
        cfg = self._agentcore.get("policy", {})
        if not cfg.get("enabled"):
            return True
        import os

        from app.features.policy import is_authorized
        engine_id = os.getenv("POLICY_ENGINE_ID", "")
        return await is_authorized(engine_id, self.agent_id, action, self.session_id)
