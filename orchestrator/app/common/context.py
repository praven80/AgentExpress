"""AgentContext — the single object handed to every agent's run() method.

It hides the plumbing (state shape, event emission, LLM/MCP clients, AgentCore
features) so an agent author only writes business logic. It also carries the
agent's configured model so ctx.llm() uses the right one per agent.

The AgentCore feature methods at the bottom are all CONFIG-DRIVEN: each reads the
agent's `agentcore` block from workflow.json and is a no-op when the feature is
disabled. An agent can therefore call them unconditionally.
"""

import contextlib
import json as _json
import re
from typing import ClassVar

from app.common.config import TOOLS
from app.common.llm import run_llm
from app.common.sink import WorkflowCancelled, emit, is_cancelled
from app.features.gateway.client import query_tool, query_tool_rows
from app.features.gateway.client import retrieve as retrieve_kb


def _slug(text: str) -> str:
    """Lowercase, hyphenated, alphanumeric-only slug (safe for a memory actorId)."""
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")


# A field name that says "this list holds what the run could not settle", in
# whatever vocabulary the workflow chose. Used by `_insight_from` to find the most
# reusable part of an asset for long-term memory WITHOUT knowing any schema: it
# previously named this sample's own `openQuestions`/`limitations`/`dataLimitations`,
# so a workflow calling the same thing `caveats`, `gaps` or `unknowns` stored only
# half of what it should. These are generic English words about uncertainty, not
# about any subject matter.
_UNCERTAIN_KEY_RE = re.compile(
    r"question|limitation|gap|unknown|caveat|missing|unresolved|outstanding"
    r"|risk|blocker|assumption|exclusion|constraint",
    re.IGNORECASE)

# Words that say nothing about the subject, so sharing one is not evidence that a
# recalled insight belongs to this run. Compared AFTER stemming, so singular forms
# are enough — the plural pairs below are harmless but no longer necessary.
_TOPIC_STOPWORDS = frozenset({
    "about", "after", "also", "with", "what", "when", "where", "which", "while",
    "your", "user", "users", "request", "requests", "build", "building", "design",
    "designing", "create", "creating", "implement", "implementing", "provide",
    "provides", "using", "used", "from", "into", "that", "this", "they", "them",
    "then", "than", "have", "has", "should", "would", "could", "will", "must",
    "need", "needs", "want", "wants", "make", "makes", "more", "most", "some",
    "such", "only", "other", "another", "over", "under", "both", "each", "many",
    "much", "very", "help",
    # GENERIC CONTAINER NOUNS — the thing being built, named in the vaguest
    # possible way. These are the words nearly every topic shares, so matching on
    # one is not evidence of a shared subject. `application` is here because of a
    # measured false match: with plurals folded, "Build a data lake application"
    # matched a recalled insight about "agentic AI applications" on that word
    # alone, which is precisely the cross-topic bleed this filter exists to stop.
    # The discriminating words in those two topics are `lake` and `agentic`.
    "application", "app", "system", "solution", "platform", "project", "software",
    "product", "tool", "thing", "stuff", "work", "task", "case",
})


def _stem(word: str) -> str:
    """Crudest useful stem: fold a regular plural onto its singular.

    Deliberately only plurals, and only the two endings that matter. `_on_topic`
    compares word SETS, so an exact-match comparison made "data lake" and "data
    lakes" share only "data" — measured live, where a recalled insight about data
    lakes failed to match a run about a data lake application. Any near-miss on
    wording is supposed to still recall; a plural was the commonest near-miss.

    Not a real stemmer on purpose. Porter-style suffix stripping would conflate
    words a reader would not ("policies" -> "polici", "analysis" -> "analysi") and
    the comparison here does not need that much: it needs "lakes" to reach "lake".
    `-ss` is excluded so "access" and "process" survive intact.
    """
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"          # policies -> policy
    if word.endswith("s") and not word.endswith("ss") and len(word) > 3:
        return word[:-1]                # lakes -> lake, pipelines -> pipeline
    return word


def _significant(text: str) -> set[str]:
    """Content words of four characters or more, minus the stopwords above, with
    regular plurals folded so a near-miss on wording still counts as a match.

    Stemmed BEFORE the stopword test, so a stopword only has to be listed in its
    singular form and "applications" is excluded by listing "application".
    """
    words = (_stem(w) for w in re.findall(r"[a-z]{4,}", (text or "").lower()))
    return {w for w in words if w not in _TOPIC_STOPWORDS}


def _on_topic(recalled: list[str], query: str) -> list[str]:
    """Recalled insights that share at least one content word with the query.

    A SECOND LINE OF DEFENCE behind namespace scoping (see _memory_actor). An
    operator who sets one `subject_id` across several topics is asking for one
    namespace, and semantic search inside it returns its top_k whether or not
    anything is actually close — which is how a pipeline run was handed insights
    about an agentic AI application.

    The bar is deliberately one shared word: a near-miss on wording should still
    recall, and the case this exists to stop shares nothing at all. With no
    significant words in the query there is nothing to compare, so everything is
    kept rather than silently discarded.
    """
    wanted = _significant(query)
    if not wanted:
        return recalled
    return [item for item in recalled if _significant(item) & wanted]


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
        with contextlib.suppress(Exception):  # observability is best-effort
            from app.features.observability.scope import set_scope
            set_scope(self.session_id, self.agent_id, self.user, self.version)
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
                + "\n(These are UNVERIFIED recollections from earlier runs, not "
                  "evidence. They may be stale, they may belong to a different "
                  "request, and they may assert things nobody said. Use them only "
                  "to orient yourself. Never present a recalled item as a sourced "
                  "fact, as a fact about the requester, or as the reason for a "
                  "conclusion: a recollection is a hint about where to look, never "
                  "support for what you found. If a recalled detail matters, it "
                  "belongs in your open questions as something to confirm.)"
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

    async def call_tool_rows(self, tool_key: str, query: str | None = None):
        """The same tool call as `call_tool`, returning (rows, field_map, mode).

        For an agent that COMPUTES its output rather than asking a model for it. It
        needs the tool's records, not the prose rendering a model reads, and it must
        not have to know which field names this particular tool uses — so:

          rows       the tool's results as dicts, exactly as the target returned
          field_map  the tool's `rowFields` block from workflow.json: the roles this
                     agent needs ("id", "label", "outcome", "timestamp") mapped onto
                     whatever THIS target calls them

        That mapping is what keeps a deterministic agent config-driven. Point the
        tool at a different Lambda, warehouse or API, set `rowFields` to its column
        names, and the agent is unchanged. Without it the agent would have to know
        one target's field names, and swapping the target would need a code edit —
        which would break the promise this framework is built on.

        `query` defaults to the retrieval query derived the same way `call_tool`
        derives it, so there is no second convention to learn. Pass it explicitly
        when the tool's argument is not a search phrase — the pricing tool wants a
        LIST OF SERVICES the agent worked out first, and sending it the run's
        objective instead would price nothing.
        """
        rows, mode = await query_tool_rows(
            tool_key, query if query is not None else self._retrieval_query())
        field_map = dict((TOOLS.get(tool_key) or {}).get("rowFields") or {})
        return rows, field_map, mode

    def _retrieval_query(self) -> str:
        """The query this agent sends its tool: the brief's objective, else the topic."""
        from app.common import assets
        return assets.brief_query(assets.brief(self), self.topic)

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
        with contextlib.suppress(Exception):
            from app.features.observability import meter
            meter.record_guardrail(source=source, action=action, detail=detail,
                                   latency_ms=latency_ms)

    # How much of a caller-supplied `subject_id` to keep in the namespace. Long
    # enough that two subjects do not collide, short enough to stay legible.
    _SUBJECT_SLUG_CHARS = 48

    def _memory_actor(self) -> str:
        """Long-term memory actor id: the agent, scoped by subject when given.

        `subject_id` is what an operator sets to group runs deliberately — a
        customer, an account, a product line — and it wins when present.

        WITHOUT ONE THE NAMESPACE IS PER AGENT, so recall has a corpus to search.
        This was derived from the TOPIC for a while, and the measurement is why it
        is not any more: across 66 writes the deployment accumulated 88 distinct
        namespaces — roughly one per run, each holding a single record — and of 101
        recalls, the only 23 that ever returned anything predate the change.
        Long-term memory had become write-only, which is worse than the problem it
        was fixing, and silently so.

        What it was fixing was real: a bare per-agent namespace once handed a
        serverless-pipeline run "the user is interested in building an agentic AI
        application" and "an analysis identified nine core architectural
        components" from an unrelated earlier topic. Partitioning was the wrong
        remedy, because two other defences now cover it and neither existed then:

          * ON WRITE, `_drop_meta_sentences` removes sentences asserting facts about
            the requester — which is what made those recalls harmful rather than
            merely irrelevant.
          * ON READ, `_on_topic` drops a recalled item sharing no significant word
            with this run's topic, which is exactly the second example above.

        And the recall itself is a SEMANTIC search keyed on the run's topic
        (`search_long_term_memories(query=topic, top_k=5)`), so relevance ranking
        by the topic is already happening — for free, and better than a word filter.
        Scoping the namespace by topic as well meant asking the same question twice
        and taking the harsher answer.

        So the default accumulates per agent and is filtered on the way out, and an
        operator who wants a harder partition sets `subject_id`.
        """
        subject = _slug(self.subject_id)[:self._SUBJECT_SLUG_CHARS]
        return f"{self.agent_id}-{subject}" if subject else self.agent_id

    def _record_memory(self, op: str, namespace: str, query: str, result_text: str,
                       latency_ms: int) -> None:
        """Best-effort: record a memory read/write for the observability UI."""
        with contextlib.suppress(Exception):
            from app.features.observability import meter
            meter.record_memory(op=op, namespace=namespace, query=query,
                                result_text=result_text, latency_ms=latency_ms)

    # Long-term strategy -> namespace prefix. Matches the strategy namespace
    # templates provisioned on the semantic memory (terraform/main.tf):
    #   semantic -> insights/{actorId}   summary -> summary/{actorId}/{sessionId}
    # Only the strategies both IaC paths actually provision (terraform/main.tf
    # semantic_memory_strategy + summary_memory_strategy, and the CDK equivalents).
    # `user_preference` was listed here without a provisioned strategy, so enabling
    # it recalled from a namespace nothing ever writes — and reported success.
    _NS_PREFIX: ClassVar[dict[str, str]] = {"semantic": "insights", "summary": "summary"}

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

        kept = _on_topic(combined, query)
        # WHAT THE AGENT ACTUALLY GOT, not what the store returned. The rows above
        # record the RAW result per namespace, and `_on_topic` runs after them — so
        # a recall that found five records and discarded all five was reported as a
        # hit, and the observability panel showed memory "working" while the agent
        # received nothing. Observed while verifying a live run: a data-lake run
        # recalled agentic-AI insights, all correctly dropped, and the panel said
        # HIT. Only recorded when the filter actually removed something, so the
        # happy path stays one row per namespace.
        if len(kept) != len(combined):
            self._record_memory(
                "recall-filtered", "", query,
                f"{len(kept)} of {len(combined)} recalled insight(s) shared a "
                f"subject word with this run; the rest were dropped as off-topic.",
                0)
        return kept

    async def memory_store(self, content: str) -> None:
        """Store an insight in long-term memory (scoped to this agent + subject)
        for future sessions. One stored turn feeds ALL enabled strategies (each
        extracts into its own namespace). No-op if disabled. Recorded for the UI.

        `content` is the agent's raw output — an asset JSON document. It is
        condensed to a short prose insight first; see _insight_from()."""
        strategies = self._longterm_strategies()
        if not strategies:
            return
        import time as _t

        from app.features.memory import store
        actor = self._memory_actor()
        insight = self._insight_from(content)
        if not insight:
            return
        start = _t.perf_counter()
        # store() writes ONE short-term event under this actor; every matching
        # strategy (semantic, summary, …) then extracts from it asynchronously.
        targets = ", ".join(self._namespace_for(s) for s in strategies)
        try:
            await store(self.session_id, actor, insight)
        except Exception as e:  # noqa: BLE001 - memory must never break a run
            self._record_memory("store", targets, "", f"(store error: {type(e).__name__})",
                                int((_t.perf_counter() - start) * 1000))
            return
        self._record_memory("store", targets, "", insight,
                            int((_t.perf_counter() - start) * 1000))

    # Fields that are bookkeeping, not knowledge. Storing the whole asset put these
    # in front of the extraction model, which is why recall came back as
    # "asset ID: asset-analysis-…-v2, version 2, status: in-review" — true, and
    # worthless to a later run.
    _INSIGHT_MAX_CHARS = 700

    # Sentences ABOUT the requester or about this system's own run history are the
    # one thing that must never reach long-term memory: a prompt rule can be
    # re-tried next run, but a poisoned memory record is recalled by every future
    # run until someone deletes it (and a run count written today is wrong
    # tomorrow). Observed writes this drops: "the user ... has completed four
    # prior runs on this subject", "the user's background (hands-on with AWS
    # serverless) suggests ...". Sentence-level, so the rest of the summary
    # survives.
    _ABOUT_REQUEST = re.compile(
        r"\b(the user|the requester|the client)\b.*\b(has|have|is|are|was|were|"
        r"prefers|knows|wants|background|experience|familiar)\b"
        r"|\b(prior|previous|earlier|past)\s+run",
        re.IGNORECASE,
    )

    @classmethod
    def _drop_meta_sentences(cls, text: str) -> str:
        """Remove sentences that assert facts about the requester or the run
        history, keeping the rest. See _ABOUT_REQUEST."""
        sentences = re.split(r"(?<=[.!?])\s+", (text or "").strip())
        kept = [s for s in sentences if s and not cls._ABOUT_REQUEST.search(s)]
        return " ".join(kept).strip()

    def _insight_from(self, content: str) -> str:
        """Condense an agent's asset output into a short prose insight worth
        recalling on a LATER run.

        Long-term memory used to receive `asset.model_dump_json()` verbatim. The
        extraction model was therefore reading a wall of JSON, and it did two bad
        things with it: it surfaced asset bookkeeping (ids, versions, statuses) as
        the "insight", and it inferred user attributes that nobody had stated —
        observed live asserting that the user "has basic familiarity with LLMs" and
        "is interested in contemporary AI approaches from 2024 onwards" for a
        request whose entire text was five words. Passing prose that names the topic
        and the open unknowns gives the strategy something true to extract, and
        makes the semantic recall query (the run's topic) match on substance.
        """
        obj = None
        try:
            parsed = _json.loads(content or "")
            obj = parsed if isinstance(parsed, dict) else None
        except (TypeError, ValueError):
            obj = None
        if obj is None:
            # Not an asset (a plain-text agent). Store it as-is, capped.
            return self._drop_meta_sentences(content)[:self._INSIGHT_MAX_CHARS]

        parts: list[str] = []
        topic = str(obj.get("title") or self.topic or "").strip()
        if topic:
            parts.append(f"Request topic: {topic}.")
        gist = self._drop_meta_sentences(
            str(obj.get("executiveSummary") or obj.get("summary") or ""))
        if gist:
            parts.append(gist)
        # What the run could NOT establish is the most reusable thing here: it tells
        # a later run what to ask for up front. Found by SHAPE and by a generic
        # uncertainty word, not by this sample's field names — those were
        # `openQuestions`/`limitations`/`dataLimitations`, so a workflow calling the
        # same thing `caveats`, `gaps` or `unknowns` silently lost half its memory.
        for key, value in obj.items():
            if not _UNCERTAIN_KEY_RE.search(key) or not isinstance(value, list):
                continue
            items = [t for t in (self._drop_meta_sentences(str(x)) for x in value) if t]
            if items:
                parts.append("Unresolved: " + "; ".join(items[:3]))
                break
        return " ".join(parts).strip()[:self._INSIGHT_MAX_CHARS]

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
