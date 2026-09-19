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

from conftest import ORCH_ROOT

# Agent-level keys, each with the module that reads it.
AGENT_KEYS = {
    "name": "registry.py -> agent.name; UI node title",
    "runtime": "registry.py (main | dedicated | a2a); subagent_runtimes.tf; UI chip",
    "agentCard": "a2a_agent.py -> the remote agent's Agent Card URL; runtime \"a2a\" only",
    "auth": "a2a_agent.py -> none | bearer | oauth2; runtime \"a2a\" only",
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
    """The keys the UI consumes must survive the BFF projection.

    Both IaC paths build a compact `ui`/`chatbot` projection for the 4 KB Lambda
    env. A key present here but dropped there is invisible to the browser, which is
    exactly how greeting/placeholder became decorative."""
    import re

    index = (ORCH_ROOT / "web" / "index.html").read_text()
    for key in UI_KEYS:
        # Any accessor: ui.<key>, uiCfg.<key>, cfg.<key>, ui["<key>"].
        assert re.search(rf"[.\[]\s*\"?{re.escape(key)}\b", index), (
            f"ui.{key} is declared in UI_KEYS but index.html never reads it")
    # chatbot.greeting / .placeholder must be projected by BOTH IaC paths.
    tf = (ORCH_ROOT / "terraform" / "bff.tf").read_text()
    cdk = (ORCH_ROOT / "cdk" / "lib" / "orchestrator-stack.ts").read_text()
    for field in ("greeting", "placeholder"):
        assert f"chatbot.{field}" in tf, f"terraform/bff.tf does not ship chatbot.{field}"
        assert f"{field}: cb.{field}" in cdk, f"the CDK path does not ship chatbot.{field}"


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
        for key in ("agentCard", "auth"):
            if key in a:
                assert remote, (
                    f"agent {aid!r} sets {key!r} but is not runtime \"a2a\"; nothing reads it")
        if remote:
            assert a.get("agentCard"), f"agent {aid!r} is runtime \"a2a\" with no agentCard"
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
