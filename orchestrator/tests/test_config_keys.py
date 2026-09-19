"""Every key in workflow.json must be one something actually reads.

This file exists because the config had drifted into carrying keys nothing
consumed. Some were harmless duplication (`agentcore.runtime.mode` restating the
agent's top-level `runtime`), but three were worse — they read like switches and
did nothing:

    agentcore.gateway.targets        looked like a per-agent tool allow-list; the
                                     real controls are the `tool` binding and the
                                     generated Cedar permit
    agentcore.observability.enabled  looked like an off switch; telemetry is
                                     captured for every agent regardless
    agentcore.memory.shortTerm       looked per-agent; the LangGraph checkpointer
                                     is deployment-wide

A config key that appears to control something and doesn't is worse than no key at
all, because a reader will believe it. So the allow-lists below are deliberately
CLOSED: adding a key to workflow.json without wiring it up fails here.

If you add a real key, add it here and note where it is read.
"""

import json

import pytest
from conftest import ORCH_ROOT

# Agent-level keys, each with the module that reads it.
AGENT_KEYS = {
    "name": "registry.py -> agent.name; UI node title",
    "kind": ("registry.py:34 -> agent.kind (\"sync\" | \"async\"); bff/workflow.py ships it; "
             "index.html renders it as a chip when it is not \"sync\""),
    "runtime": "registry.py (main | dedicated | a2a); subagent_runtimes.tf; UI chip",
    "agentCard": "a2a_agent.py -> the remote agent's Agent Card URL; runtime \"a2a\" only",
    "auth": "a2a_agent.py -> none | bearer | oauth2 | sigv4; runtime \"a2a\" only",
    "source": ("registry.py A2A_SOURCES -> the stand-in A2A server the IaC deploys "
               "and injects a URL for; runtime \"a2a\" only, exclusive with agentCard"),
    "skill": ("a2a.tf / orchestrator-stack.ts -> the path segment of the injected "
              "endpoint, selecting which of the stand-in's reviewers this agent is; "
              "only with `source`"),
    "model": "registry.py -> agent.model (omit to use orchestrator.defaultModel)",
    "maxTokens": "registry.py -> agent.max_tokens, the model output budget",
    "temperature": "registry.py -> agent.temperature",
    "tool": "registry.py -> agent.tool; validated against the tools block",
    "corpus": "registry.py -> agent.corpus; validated against tools.<kb>.corpora",
    "produces": "nodes.py, injected into the agent's task prompt",
    "access": "UI data-source chip, ONLY for an agent with no `tool`",
    "agentcore": "the feature block, keys below",
}

# agentcore.* keys, dotted, each with its reader.
AGENTCORE_KEYS = {
    "memory.longTerm": "context.py:200 - recall/store across runs",
    "identity.outbound": "context.py:272 - OAuth providers ctx.get_identity_token may use",
    "guardrails.input": "context.py:146 - Bedrock guardrail before the model call",
    "guardrails.output": "context.py:146 - Bedrock guardrail after the model call",
    "evaluations.enabled": "evaluations/service.py:is_enabled",
    "evaluations.auto": "evaluations/service.py:is_auto",
    "evaluations.evaluators": "evaluations/service.py:evaluators_for",
    "policy.enabled": "context.py:policy_check",
}

TOP_LEVEL = {
    "$comment", "orchestrator", "ui", "guardrail", "authorization", "tools",
    "agents", "steps",
}

ORCHESTRATOR_KEYS = {
    "defaultModel": "config.py:MODEL_ID fallback",
    "modelRates": ("config.py:MODEL_RATES -> observability/pricing.py; per-model token "
                   "rates so a customer's own model is costed correctly without a "
                   "framework edit. Unknown models are marked rates_known=False"),
    "insights": ("config.py:INSIGHTS -> features/optimization/insights.py "
                 "(lookbackHours, pollTimeoutSeconds, pollIntervalSeconds)"),
    "runtimeInvoke": "config.py:RUNTIME_INVOKE -> agentcore_agent._agentcore (SDK retries/timeout)",
    "a2aInvoke": "config.py:A2A_INVOKE -> a2a_agent (request timeout, poll interval, poll budget)",
    "policy": "policy.tf / tool-plane.ts - Cedar engine on/off + mode",
    "chatbot": "bff/chatbot.py + the UI gate",
}
# Presentation strings. Every one must have a reader, for the same reason as the
# rest: `chatbot.greeting` and `chatbot.placeholder` sat in this file for a while
# WITHOUT being shipped in the BFF projection, so a customer could edit them and the
# UI would keep showing its own hardcoded copy. That is the failure mode this whole
# module exists to catch, and `ui` was the one block it did not cover.
UI_KEYS = {
    "title": "index.html:applyUiConfig -> document.title",
    "heading": "index.html:applyUiConfig -> header h1",
    "defaultTopic": "config.py:DEFAULT_TOPIC, bff/handler.py:DEFAULT_TOPIC, applyUiConfig",
    "topicPlaceholder": "index.html:applyUiConfig -> #topic placeholder + aria-label",
    "subjectPlaceholder": "index.html:applyUiConfig -> #subject placeholder + aria-label",
    "subjectHint": "index.html:applyUiConfig -> #subject title",
    "assistantTitle": "index.html:initChatbot -> assistant panel title",
    "assistantSubtitle": "index.html:initChatbot -> assistant panel subtitle",
}


def wf() -> dict:
    return json.loads((ORCH_ROOT / "app" / "workflow.json").read_text())


def dotted(o, prefix=""):
    out = set()
    if isinstance(o, dict):
        for k, v in o.items():
            key = f"{prefix}.{k}" if prefix else k
            out.add(key)
            out |= dotted(v, key)
    return out


def test_no_unread_agent_keys():
    for aid, a in wf()["agents"].items():
        unknown = set(a) - set(AGENT_KEYS)
        assert not unknown, (
            f"agent {aid!r} has key(s) nothing reads: {sorted(unknown)}. Either wire "
            f"them up or remove them — see this file's docstring.")


def test_no_unread_agentcore_keys():
    for aid, a in wf()["agents"].items():
        ac = a.get("agentcore", {})
        found = {k for k in dotted(ac) if "." in k}
        # Block names on their own (e.g. "memory") are containers, not settings.
        unknown = found - set(AGENTCORE_KEYS)
        assert not unknown, (
            f"agent {aid!r} agentcore has key(s) nothing reads: {sorted(unknown)}.")


def test_no_unread_ui_keys():
    """Every `ui` string must reach the page. See UI_KEYS for why this exists."""
    unknown = set(wf().get("ui", {})) - set(UI_KEYS)
    assert not unknown, (
        f"ui has key(s) nothing reads: {sorted(unknown)}. Either render them or "
        f"remove them — a presentation key that does nothing is worse than absent, "
        f"because a customer edits it and sees no change.")


def test_ui_strings_shipped_to_the_browser_are_actually_read_there():
    """The keys the UI consumes must survive the BFF projection, in both directions.

    A `ui` key the page never reads is decorative. A key the page reads but the
    projection drops is worse: the customer edits it and the UI keeps showing its own
    hardcoded copy, which is exactly what happened to the assistant's greeting and
    placeholder.

    This used to grep both IaC languages for the field names, because the projection
    was built twice — in HCL and in TypeScript — and either copy could drop one. It
    is one Python function now (bff/workflow.py), so the assertion is made against
    its actual OUTPUT rather than against the text of two templates.
    """
    import re
    import sys

    index = (ORCH_ROOT / "web" / "index.html").read_text()
    for key in UI_KEYS:
        # Any accessor: ui.<key>, uiCfg.<key>, cfg.<key>, ui["<key>"].
        assert re.search(rf"[.\[]\s*\"?{re.escape(key)}\b", index), (
            f"ui.{key} is declared in UI_KEYS but index.html never reads it")

    sys.modules.pop("workflow", None)
    from workflow import project

    shipped = project(wf())
    for key, value in (wf().get("ui") or {}).items():
        assert shipped["ui"].get(key) == value, (
            f"ui.{key} is in workflow.json but the BFF projection does not ship it, "
            f"so the browser cannot see it")
    for field in ("greeting", "placeholder"):
        configured = (wf()["orchestrator"].get("chatbot") or {}).get(field)
        assert shipped["chatbot"][field] == configured, (
            f"chatbot.{field} is configured but not projected to the browser")


def test_no_note_or_description_prose_in_agents():
    """Explanatory prose belongs in docs/WORKFLOW_REFERENCE.md, not in the config.

    It was stripped before shipping anyway, so it only ever bloated the file the
    customer is told to edit."""
    for aid, a in wf()["agents"].items():
        prose = [k for k in dotted(a) if k.lower().endswith("note")
                 or k.split(".")[-1] == "description"]
        assert not prose, f"agent {aid!r} carries prose keys: {prose}"


def test_no_empty_blocks_or_redundant_false():
    """An empty block and an `enabled: false` both mean exactly what absence means,
    so they are noise the reader has to parse."""
    for aid, a in wf()["agents"].items():
        for block, v in (a.get("agentcore") or {}).items():
            assert v != {}, f"agent {aid!r} has an empty agentcore.{block}"
            assert v != {"enabled": False}, (
                f"agent {aid!r} agentcore.{block} is 'enabled: false', which is "
                f"identical to omitting the block")
        assert a.get("agentcore") != {}, f"agent {aid!r} has an empty agentcore block"


def test_access_only_on_agents_without_a_tool():
    """`access` feeds the UI chip, and both projections check `tool` FIRST — so on a
    tool-bound agent it is never read."""
    for aid, a in wf()["agents"].items():
        if a.get("tool"):
            assert "access" not in a, (
                f"agent {aid!r} has both `tool` and `access`; `access` is unread when "
                f"a tool is set (the chip comes from the tool's type)")


def test_top_level_and_orchestrator_keys_are_known():
    w = wf()
    assert set(w) == TOP_LEVEL, f"unexpected top-level keys: {set(w) ^ TOP_LEVEL}"
    unknown = set(w["orchestrator"]) - set(ORCHESTRATOR_KEYS)
    assert not unknown, f"orchestrator has unread key(s): {sorted(unknown)}"


def _local_agents() -> dict:
    """Agents whose model call this deployment makes. A `runtime: "a2a"` agent is
    someone else's service: it chooses its own model and owns its own token budget,
    so the model settings do not apply to it (see registry.validate_runtimes)."""
    return {aid: a for aid, a in wf()["agents"].items()
            if str(a.get("runtime") or "main") != "a2a"}


def test_every_agent_declares_a_token_budget():
    """Without one it silently falls back to a framework default, which is how the
    budget ended up hardcoded at each call site in the first place."""
    for aid, a in _local_agents().items():
        assert isinstance(a.get("maxTokens"), int) and a["maxTokens"] > 0, (
            f"agent {aid!r} needs a positive integer maxTokens")


def test_max_tokens_reaches_the_agent_objects():
    """The config value must actually arrive on the Agent, not just sit in the file."""
    from app.orchestrator.registry import load_agents

    conf = _local_agents()
    for aid, agent in load_agents().items():
        if aid not in conf:
            continue
        assert agent.max_tokens == conf[aid]["maxTokens"], (
            f"{aid}: config says {conf[aid]['maxTokens']} but the agent got "
            f"{agent.max_tokens}")


def test_remote_agent_keys_appear_only_on_a_remote_agent():
    """`agentCard` and `auth` are read by A2AAgent alone. On a `main` or `dedicated`
    agent they would look like settings and control nothing — the failure this whole
    module exists to prevent. Enforced at container start too, by
    registry.validate_runtimes; asserted here against the shipped file."""
    for aid, a in wf()["agents"].items():
        remote = str(a.get("runtime") or "main") == "a2a"
        for key in ("agentCard", "auth", "source"):
            if key in a:
                assert remote, (
                    f"agent {aid!r} sets {key!r} but is not runtime \"a2a\"; nothing reads it")
        if remote:
            assert bool(a.get("agentCard")) != bool(a.get("source")), (
                f"agent {aid!r} is runtime \"a2a\" and needs exactly one of agentCard (an agent "
                f"that already exists) or source (the stand-in this repo deploys)")
            for key in ("model", "temperature", "maxTokens", "tool", "corpus"):
                assert key not in a, (
                    f"agent {aid!r} is runtime \"a2a\" and also sets {key!r}; a remote agent "
                    f"makes its own model call and reaches its own data sources")


def test_no_agent_hardcodes_a_token_budget():
    """The budget is config. A LITERAL at a call site puts it back in code, where it
    is invisible to whoever edits workflow.json.

    A pass-through (`max_tokens=max_tokens`, `max_tokens=None`) is fine and is how
    the shared synthesis runner lets one caller override for one call; what must
    never appear is a number.
    """
    import re

    literal = re.compile(r"max_tokens\s*=\s*\d")
    for path in (ORCH_ROOT / "app" / "subagents").rglob("*.py"):
        hit = literal.search(path.read_text())
        assert not hit, (
            f"{path.relative_to(ORCH_ROOT)} hardcodes {hit.group()!r}; let it default "
            f"to the agent's configured maxTokens instead")


# ---------------------------------------------------------------------------
# A recognised KEY with an unrecognised VALUE
# ---------------------------------------------------------------------------
# The allow-lists above close the set of agentcore KEYS. These close the set of
# VALUES for the two that were silent when wrong — the same class of bug one level
# down, and the reason `registry.validate_features` exists.

def _one_agent(agentcore: dict) -> dict:
    return {
        "orchestrator": {"defaultModel": "m"},
        "agents": {"solo": {"name": "Solo", "maxTokens": 100, "agentcore": agentcore}},
        "steps": [{"agent": "solo"}],
    }


def test_an_unprovisioned_memory_strategy_is_rejected():
    """It used to be SILENT. `_namespace_for` falls back to the strategy name as its
    own prefix, so this searched a namespace nothing writes, found nothing, and
    reported a successful recall — with the observability panel showing memory
    working."""
    from conftest import workflow

    with workflow(_one_agent({"memory": {"longTerm": ["userPreference"]}})) as imp:
        registry = imp("app.orchestrator.registry")
        with pytest.raises(ValueError, match="long-term memory strategy"):
            registry.validate_features()


def test_the_provisioned_memory_strategies_are_accepted():
    from conftest import workflow

    with workflow(_one_agent({"memory": {"longTerm": ["semantic", "summary"]}})) as imp:
        imp("app.orchestrator.registry").validate_features()


def test_a_bare_truthy_long_term_still_means_semantic():
    """`longTerm: true` is documented shorthand (context._longterm_strategies), so it
    must not be dragged through the list check."""
    from conftest import workflow

    with workflow(_one_agent({"memory": {"longTerm": True}})) as imp:
        imp("app.orchestrator.registry").validate_features()


def test_a_misspelled_evaluator_is_rejected_at_start_not_at_evaluation_time():
    """AgentCore rejects an unknown evaluator when an evaluation RUNS, which can be
    days after the deploy reported success."""
    from conftest import workflow

    with workflow(_one_agent({"evaluations": {
            "enabled": True, "evaluators": ["Builtin.Faithfullness"]}})) as imp:
        registry = imp("app.orchestrator.registry")
        # Caught on shape, not on a name list — see below.
        registry.validate_features()  # "Faithfullness" is a valid SHAPE, so this passes

    with workflow(_one_agent({"evaluations": {
            "enabled": True, "evaluators": ["Faithfulness"]}})) as imp:
        registry = imp("app.orchestrator.registry")
        with pytest.raises(ValueError, match="Builtin"):
            registry.validate_features()


def test_evaluator_validation_is_a_shape_check_not_a_name_list():
    """Deliberate: AWS adds built-in evaluators, and a closed list here would reject a
    valid new one and force a framework edit to use it. What is caught is the
    structural mistake — a bare name, a lower-case prefix, a typo'd namespace."""
    from conftest import workflow

    with workflow(_one_agent({"evaluations": {
            "enabled": True,
            "evaluators": ["Builtin.SomeEvaluatorAwsAddedLastWeek",
                           "Custom.OurOwnRubric"]}})) as imp:
        imp("app.orchestrator.registry").validate_features()

    for bad in ("builtin.Faithfulness", "Builtin.", "Builtin", "Buitlin.Faithfulness"):
        with workflow(_one_agent({"evaluations": {
                "enabled": True, "evaluators": [bad]}})) as imp:
            registry = imp("app.orchestrator.registry")
            with pytest.raises(ValueError, match="Builtin"):
                registry.validate_features()


def test_the_shipped_workflow_passes_feature_validation():
    from app.orchestrator.registry import validate_features

    validate_features()


# ---------------------------------------------------------------------------
# The framework vocabulary has ONE home
# ---------------------------------------------------------------------------
# Every closed value set used to be written out two or three times — in Python, in
# cdk/lib/*.ts and in terraform/*.tf. `toolTypes` and the RBAC action names existed in
# all three. Adding a value meant finding every copy, and a copy that got missed
# rejected a config the other planes accepted, so whether a workflow deployed depended
# on which IaC path you used. app/vocabulary.json is the single source now.

def _vocab() -> dict:
    return json.loads((ORCH_ROOT / "app" / "vocabulary.json").read_text())


def test_the_python_plane_reads_the_vocabulary_file():
    from app.common import vocabulary

    raw = _vocab()
    assert tuple(raw["runtimes"]["values"]) == vocabulary.RUNTIMES
    assert tuple(raw["toolTypes"]["values"]) == vocabulary.TOOL_TYPES
    assert tuple(raw["memoryStrategies"]["values"]) == vocabulary.MEMORY_STRATEGIES
    assert raw["builtinLambdaSource"]["values"][0] == vocabulary.BUILTIN_LAMBDA_SOURCE


def test_the_registry_validates_against_the_file_not_its_own_copy():
    """A local tuple would pass every other test here while drifting from the IaC."""
    from app.common import vocabulary
    from app.orchestrator import registry

    assert registry.RUNTIMES is vocabulary.RUNTIMES
    assert registry.TOOL_TYPES is vocabulary.TOOL_TYPES
    assert registry.MEMORY_STRATEGIES is vocabulary.MEMORY_STRATEGIES
    assert registry.A2A_SOURCES is vocabulary.A2A_SOURCES
    assert registry.A2A_LAMBDA_SKILLS is vocabulary.A2A_LAMBDA_SKILLS
    assert registry.AUTH_MODES is vocabulary.A2A_AUTH_MODES


def test_an_undeclared_vocabulary_raises_rather_than_returning_an_empty_set():
    """An empty set would make every value invalid, or every value valid, depending on
    how the caller uses it. Neither is a safe silent default."""
    from app.common import vocabulary

    with pytest.raises(KeyError, match="not a vocabulary"):
        vocabulary.values("thingsThatDoNotExist")


def test_every_declared_set_is_non_empty_and_explains_why_it_is_closed():
    """A closed set a customer can trip over must say WHY, or its error message is a
    dead end."""
    for name, block in _vocab().items():
        if name.startswith("$"):
            continue
        assert block.get("values"), f"{name} declares no values"
        assert len(str(block.get("$comment") or "")) > 20, (
            f"{name} does not explain why it is closed")


def test_the_vocabulary_is_framework_owned_not_customer_owned():
    """It is deliberately NOT part of workflow.json: a customer's file must not be able
    to widen a set the framework enforces."""
    wf = wf_raw = json.loads((ORCH_ROOT / "app" / "workflow.json").read_text())
    assert "vocabulary" not in wf
    for key in _vocab():
        if key.startswith("$"):
            continue
        assert key not in wf_raw, f"{key} leaked into the customer's workflow.json"
    # And it must say so, because it sits next to workflow.json.
    assert "NOT a file a customer edits" in _vocab()["$comment"]


def test_the_shipped_workflow_only_uses_declared_values():
    """The point of the file: every value the sample uses is in the vocabulary, so all
    three planes accept it."""
    from app.common import vocabulary

    w = wf()
    for aid, agent in w["agents"].items():
        assert (agent.get("runtime") or "main") in vocabulary.RUNTIMES, aid
        if agent.get("auth"):
            assert agent["auth"] in vocabulary.A2A_AUTH_MODES, aid
        if agent.get("source"):
            assert agent["source"] in vocabulary.A2A_SOURCES, aid
        if agent.get("skill"):
            assert agent["skill"] in vocabulary.A2A_LAMBDA_SKILLS, aid
        for strategy in ((agent.get("agentcore") or {}).get("memory") or {}).get("longTerm") or []:
            assert strategy in vocabulary.MEMORY_STRATEGIES, aid
    for label, tool in (w.get("tools") or {}).items():
        assert tool["type"] in vocabulary.TOOL_TYPES, label
    for action in (w.get("authorization") or {}).get("actions") or {}:
        assert action in vocabulary.AUTHORIZATION_ACTIONS, action
