"""The promise, tested: a DIFFERENT use case needs no framework edit.

The claim this framework makes is narrow and worth pinning down: to build something
else, a customer writes `app/workflow.json` and puts their agent logic under
`app/subagents/<id>/`. Nothing under `app/common/`, `app/orchestrator/`,
`app/features/`, `bff/` or the IaC should need touching.

So this file builds a workflow with nothing in common with the shipped sample —
insurance claims triage, different agent ids, a different topology, a different
gate layout, its own asset type, its own contract, and NO use of the sample's shared
runners in `app/subagents/_shared/` — and asserts the framework carries it.

It exists because the framework used to fail this test in ways that were invisible
from inside the sample:

  * `rules.py` checked every payload against one editorial standard built for AWS
    research reports. `_ARTIFACT_NOUNS` contained "document", "record", "evidence"
    and "transcript", so a claims or legal workflow got its most useful sentences
    reported as defects. Deleted.
  * `AssetEnvelope` carried `ruleViolations`, a field only that engine wrote.
  * `AssetType` was an enum of the sample's five names, living in the framework.
  * The long-term-memory prompt injected into EVERY agent illustrated its point
    with "the user's background (hands-on with AWS serverless, Lambda, DynamoDB)".
  * The memory insight extractor looked for `openQuestions` / `limitations` /
    `dataLimitations` by name, so a workflow using its own words silently stored
    half of what it should.

None of that was reachable from the sample's own tests, because the sample agreed
with all of it.
"""

from __future__ import annotations

import json
import shutil

import pytest
from conftest import ORCH_ROOT, workflow

# A claims-triage workflow. No agent id, asset type, tool or gate name is shared
# with the shipped sample, and no step shape is copied from it either: a sequence
# feeding a parallel pair, one gate, at the end rather than the start.
CLAIMS_WF = {
    "orchestrator": {"defaultModel": "us.anthropic.claude-haiku-4-5-20251001-v1:0"},
    "ui": {"title": "Claims Triage", "defaultTopic": "Assess claim 88213"},
    "tools": {},
    "agents": {
        "fnol": {"name": "First Notice of Loss", "runtime": "main", "maxTokens": 2000,
                 "produces": "loss report"},
        "policy_check": {"name": "Policy Check", "runtime": "main", "maxTokens": 2000,
                         "produces": "coverage position"},
        "fraud_screen": {"name": "Fraud Screen", "runtime": "main", "maxTokens": 2000,
                         "produces": "fraud indicators"},
        "adjust": {"name": "Adjustment", "runtime": "main", "maxTokens": 3000,
                   "produces": "settlement decision"},
    },
    "steps": [
        {"sequence": ["fnol", "policy_check"]},
        {"parallel": ["fraud_screen", "adjust"], "gateId": "review",
         "gateName": "Claims Review", "hitl": True},
    ],
}

# The customer's own agent. It uses ctx.llm and the ENVELOPE, and imports nothing
# from app/subagents/_shared/ — that directory is the sample's, not the framework's.
AGENT_SRC = '''
"""A customer agent: its own contract, its own asset type, no shared runner."""
from __future__ import annotations

import json
from typing import Literal

from pydantic import Field

from app.common.base import Agent
from app.common.contracts import AssetEnvelope, AssetStatus, Base


class Deduction(Base):
    reason: str
    amount_gbp: float = Field(alias="amountGbp")


class SettlementDecision(AssetEnvelope):
    """A shape the framework has never heard of."""
    asset_type: Literal["settlement-decision"] = Field(
        default="settlement-decision", alias="assetType")
    disposition: str
    settlement_gbp: float = Field(alias="settlementGbp")
    deductions: list[Deduction] = Field(default_factory=list)
    unresolved_queries: list[str] = Field(default_factory=list,
                                          alias="unresolvedQueries")


class AdjustAgent(Agent):
    system_prompt = "You are a claims adjuster."

    async def run(self, ctx) -> str:
        raw = await ctx.llm(self.system_prompt, f"Assess: {ctx.topic}")
        payload = json.loads(raw)
        asset = SettlementDecision(
            assetId="asset-settlement-88213-v1",
            version=1,
            status=AssetStatus.IN_REVIEW,
            createdAt="2026-09-18 12:00:00",
            createdByAgent=ctx.agent_id,
            executiveSummary=payload["executiveSummary"],
            disposition=payload["disposition"],
            settlementGbp=payload["settlementGbp"],
            deductions=payload.get("deductions") or [],
            unresolvedQueries=payload.get("unresolvedQueries") or [],
        )
        return json.dumps(asset.model_dump(by_alias=True, mode="json"))


agent = AdjustAgent()
'''

MODEL_REPLY = json.dumps({
    "executiveSummary": "Claim 88213 is payable in part; storm damage is covered.",
    "disposition": "partially-approved",
    "settlementGbp": 4820.5,
    "deductions": [{"reason": "Policy excess", "amountGbp": 250.0}],
    "unresolvedQueries": ["The loss adjuster's site photographs were not supplied."],
})


@pytest.fixture()
def claims_packages():
    """Create the four agent packages a customer would write, then remove them."""
    made = []
    try:
        for aid in CLAIMS_WF["agents"]:
            d = ORCH_ROOT / "app" / "subagents" / aid
            d.mkdir(parents=True, exist_ok=False)
            made.append(d)
            (d / "__init__.py").write_text("from .agent import agent\n")
            (d / "agent.py").write_text(AGENT_SRC)
        yield
    finally:
        for d in made:
            shutil.rmtree(d, ignore_errors=True)


def test_a_foreign_workflow_builds_its_graph_and_loads_its_agents(claims_packages):
    """Topology, gates and the registry are derived, so unknown ids are fine."""
    with workflow(CLAIMS_WF) as imp:
        cfg = imp("app.common.config")
        assert cfg.FIRST_AGENT_ID == "fnol"
        assert cfg.LAST_AGENT_ID == "adjust"
        # A sequence member sees the one before it; parallel peers do not see each other.
        assert cfg.upstream_of("policy_check") == ["fnol"]
        assert set(cfg.upstream_of("adjust")) == {"fnol", "policy_check"}
        assert "fraud_screen" not in cfg.upstream_of("adjust")

        registry = imp("app.orchestrator.registry").load_agents()
        assert sorted(registry) == ["adjust", "fnol", "fraud_screen", "policy_check"]
        assert registry["adjust"].max_tokens == 3000  # config reached the object

        graph = imp("app.orchestrator.graph_builder").build_graph()
        nodes = set(graph.get_graph().nodes)
        assert {"fnol", "policy_check", "fraud_screen", "adjust"} <= nodes
        # The gate is named from `gateId`, not from any agent id.
        assert "review_gate" in nodes


def test_a_customer_asset_the_framework_has_never_seen_round_trips(claims_packages):
    """The envelope is the only shape the framework fixes; the rest is the customer's."""
    import asyncio

    with workflow(CLAIMS_WF) as imp:
        agent = imp("app.orchestrator.registry").build_agent_module("adjust")

        class Ctx:
            agent_id = "adjust"
            topic = "Assess claim 88213"

            async def llm(self, *_a, **_k):
                return MODEL_REPLY

        out = json.loads(asyncio.run(agent.run(Ctx())))

    assert out["assetType"] == "settlement-decision"
    assert out["settlementGbp"] == 4820.5
    assert out["deductions"] == [{"reason": "Policy excess", "amountGbp": 250.0}]
    # The framework neither added nor demanded anything of its own beyond the envelope.
    assert "ruleViolations" not in out
    for envelope_field in ("assetId", "version", "status", "createdAt", "createdByAgent"):
        assert envelope_field in out


def test_the_customers_own_unresolved_field_reaches_long_term_memory(claims_packages):
    """`unresolvedQueries` is not a name the framework knows.

    The insight extractor used to look for `openQuestions`/`limitations`/
    `dataLimitations`, so this workflow would have remembered the gist and dropped
    the open queries — the most reusable half.
    """
    with workflow(CLAIMS_WF) as imp:
        AgentContext = imp("app.common.context").AgentContext
        ctx = AgentContext.__new__(AgentContext)
        ctx.topic = "Assess claim 88213"
        insight = ctx._insight_from(json.dumps({
            "assetId": "asset-settlement-88213-v1",
            "assetType": "settlement-decision",
            "executiveSummary": "Claim 88213 is payable in part.",
            "unresolvedQueries": ["The site photographs were not supplied."],
        }))

    assert "Claim 88213 is payable in part." in insight
    assert "Unresolved: The site photographs were not supplied." in insight
    assert "asset-settlement" not in insight  # bookkeeping still excluded


def test_the_framework_does_not_import_the_sample():
    """The seam, asserted rather than assumed.

    `app/subagents/_shared/` holds the SAMPLE's runners and contracts. If a
    framework module imports them, deleting the sample breaks the framework and the
    promise is false.
    """
    offenders = []
    for area in ("common", "orchestrator", "features"):
        for path in (ORCH_ROOT / "app" / area).rglob("*.py"):
            for i, line in enumerate(path.read_text().splitlines(), 1):
                s = line.strip()
                if s.startswith(("import ", "from ")) and "subagents" in s:
                    offenders.append(f"{path.relative_to(ORCH_ROOT)}:{i}: {s}")
    assert not offenders, "framework imports the sample:\n" + "\n".join(offenders)


def test_no_framework_module_names_a_shipped_agent_in_executable_code():
    """A hardcoded agent id would break the moment a customer renames or removes it.

    Comments and docstrings are allowed to use the sample's ids as examples; a
    string literal in executable code is not.
    """
    import ast

    sample_ids = {"intake", "web_search", "knowledge_research", "documentation_search",
                  "cost_research", "analysis", "recommendation", "report"}
    offenders = []
    for area in ("common", "orchestrator"):
        for path in (ORCH_ROOT / "app" / area).rglob("*.py"):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                        and node.value in sample_ids):
                    offenders.append(
                        f"{path.relative_to(ORCH_ROOT)}:{node.lineno}: {node.value!r}")
    assert not offenders, ("framework code hardcodes a sample agent id:\n"
                           + "\n".join(offenders))
