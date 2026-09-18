"""A response that ran out of room must never look like a finished one.

`assets.extract_json` deliberately repairs JSON that was cut off mid-object, so a
model call that hit its `maxTokens` ceiling still yields a usable partial asset
instead of failing the run. That is the right trade — a report missing its last
source beats no report — but it has a nasty property: the repaired payload VALIDATES.
The asset looks complete, the run reports success, and the only remaining evidence
that anything was lost is the stop reason.

Observed live on "Build agentic ai app": the `analysis` call stopped at exactly its
6000-token budget, mid-way through writing a `sources` entry. The timeline said
"Analysis complete (v1)". The reviewer approved it at the gate. Nothing anywhere said
the tail was missing.

So: `run_llm` reports whether the response was truncated, `ctx.llm` puts it on the
run timeline and records it on the context, and each agent puts it on its asset using
that contract's own field for "something here is not sound".
"""

from __future__ import annotations

import asyncio

import pytest


class _Msg:
    """Enough of a ChatBedrockConverse response for run_llm."""

    def __init__(self, text: str, stop: str):
        self.content = text
        self.response_metadata = {"stopReason": stop}
        self.usage_metadata = {"input_tokens": 10, "output_tokens": 20}


def _run_llm_with(stop_reason: str, monkeypatch, text: str = '{"summary": "x"}'):
    """Call the REAL run_llm with a stand-in for the Bedrock client.

    `langchain_aws` is a container dependency and is deliberately not installed for
    this suite (no AWS, no model, runs in seconds), and `run_llm` imports it inside the
    function — so a stub module in sys.modules is enough to exercise the real code path.
    """
    import sys
    import types

    from app.common import llm as llm_mod

    class _FakeLLM:
        def __init__(self, **_kw):
            pass

        async def ainvoke(self, _messages):
            return _Msg(text, stop_reason)

    fake = types.ModuleType("langchain_aws")
    fake.ChatBedrockConverse = _FakeLLM
    monkeypatch.setitem(sys.modules, "langchain_aws", fake)
    return asyncio.run(llm_mod.run_llm("agent", "sys", "user", max_tokens=100))


def test_run_llm_reports_a_response_that_hit_the_ceiling(monkeypatch):
    text, truncated = _run_llm_with("max_tokens", monkeypatch)
    assert text == '{"summary": "x"}'
    assert truncated is True


def test_run_llm_reports_a_normal_completion_as_not_truncated(monkeypatch):
    _text, truncated = _run_llm_with("end_turn", monkeypatch)
    assert truncated is False


@pytest.mark.parametrize("stop", ["max_tokens", "MAX_TOKENS", "length", "max_token"])
def test_the_other_providers_spellings_count_too(stop, monkeypatch):
    """langchain-aws fronts several providers and `model` is a workflow.json value, so
    a model swap must not quietly turn this check off."""
    _text, truncated = _run_llm_with(stop, monkeypatch)
    assert truncated is True, stop


def _ctx(truncated: bool):
    """An AgentContext wired so ctx.llm runs for real against a stubbed run_llm."""
    from app.common.context import AgentContext

    c = AgentContext.__new__(AgentContext)
    c.agent_id, c.model, c.temperature, c.max_tokens = "analysis", None, 0, 6000
    c.recalled_memory, c.truncated_calls = [], []
    c.logged: list[str] = []

    async def _log(msg):
        c.logged.append(msg)

    c.log = _log
    return c


def test_ctx_llm_puts_truncation_on_the_timeline(monkeypatch):
    """The reviewer's only chance to notice is the run timeline."""
    from app.common import context as ctx_mod

    async def fake_run_llm(*_a, **_k):
        return "partial", True

    monkeypatch.setattr(ctx_mod, "run_llm", fake_run_llm)
    c = _ctx(True)
    out = asyncio.run(c.llm("sys", "user"))

    assert out == "partial"
    assert c.truncated_calls == ["analysis"]
    assert len(c.logged) == 1
    line = c.logged[0]
    assert "6000" in line, line          # the budget it hit, so the fix is actionable
    assert "cut off" in line
    assert "maxTokens" in line           # names the config key to change


def test_ctx_llm_stays_quiet_on_a_normal_completion(monkeypatch):
    from app.common import context as ctx_mod

    async def fake_run_llm(*_a, **_k):
        return "whole", False

    monkeypatch.setattr(ctx_mod, "run_llm", fake_run_llm)
    c = _ctx(False)
    assert asyncio.run(c.llm("sys", "user")) == "whole"
    assert c.truncated_calls == []
    assert c.logged == []


def test_the_shared_helper_produces_a_limitation_only_when_truncated():
    from app.subagents._shared import synthesis

    assert synthesis.truncation_limitation({"truncated": []}) == []
    assert synthesis.truncation_limitation({}) == []
    note = synthesis.truncation_limitation({"truncated": ["analysis"]})
    assert len(note) == 1
    assert "cut off" in note[0] and "maxTokens" in note[0]


def _run_report_agent(monkeypatch, *, truncated: list[str]):
    """Drive the REAL ReportAgent with synthesis.synthesize stubbed.

    Deliberately not a reimplementation of the agent's completeness rule: an earlier
    version of this test recomputed `is_complete` inline and therefore passed even when
    the agent stopped checking truncation at all. Mutating the agent must fail here.
    """
    import json

    from app.subagents._shared import synthesis

    # `app.subagents.report.agent` is the Agent INSTANCE, not the module: the package's
    # __init__ does `from .agent import agent`, which shadows the submodule name.
    from app.subagents.report import agent as report_agent
    from app.subagents.report.prompts import SECTIONS

    payload = {
        "title": "T",
        "executiveSummary": "s",
        "sections": [{"sectionType": t, "title": t, "content": "x"} for t in SECTIONS],
    }
    meta = {"title": "T", "version": 1, "brief": {}, "upstream_asset_ids": [],
            "degraded": False, "truncated": truncated}

    async def fake_synthesize(_ctx, **_kw):
        return payload, meta

    monkeypatch.setattr(synthesis, "synthesize", fake_synthesize)

    class Ctx:
        agent_id = "report"

    return json.loads(asyncio.run(report_agent.run(Ctx())))


def test_a_truncated_report_is_not_complete(monkeypatch):
    """`isComplete` is what the UI and the reviewer read as "this is ready", and the
    repaired JSON validates — so this flag is the last thing standing between a
    truncated report and an approval."""
    asset = _run_report_agent(monkeypatch, truncated=["report"])
    assert asset["isComplete"] is False, (
        "every required section was present, so the ONLY reason this must not be "
        "complete is that the response was cut off")


def test_an_untruncated_report_with_every_section_is_complete(monkeypatch):
    """The other half — or the rule above would be satisfied by always returning False."""
    asset = _run_report_agent(monkeypatch, truncated=[])
    assert asset["isComplete"] is True


def test_every_agent_that_builds_an_asset_surfaces_truncation():
    """The point is that NO agent can forget. Each contract has a different field for
    it, so this checks the wiring exists rather than the wording."""
    import re

    from conftest import ORCH_ROOT

    expected = {
        "intake": "assumptions",           # what a reviewer should check
        "analysis": "limitations",
        "recommendation": "risks",
        "report": "isComplete",            # a truncated report is not ready
        "cost_research": "dataLimitations via limits",
    }
    for agent in expected:
        src = (ORCH_ROOT / "app" / "subagents" / agent / "agent.py").read_text()
        assert re.search(r"truncat", src), f"{agent} does not mention truncation"
    # The four research agents go through the shared runner.
    shared = (ORCH_ROOT / "app" / "subagents" / "_shared" / "research.py").read_text()
    assert "truncated_calls" in shared
