"""The Cost Research agent: judgement to the model, arithmetic to code.

Before this agent existed, every run of this workflow reported that no cost figures
were supplied — correctly, because nothing supplied any. That leaves an agent two
options: say nothing useful about cost, or invent a number. The second is the
dangerous one, because a fabricated "$50,000 budget" is indistinguishable from a
real estimate once it is in a report.

So the work is split, and these tests pin the split rather than the prose:

  MODEL   "which AWS services would this use case need?" — genuinely semantic.
  CODE    "what do those services cost?" — the tool, reported verbatim.

The model is never shown a price. It cannot round one, mis-transcribe one, or
multiply two together, because by the time prices exist its work is finished. That
is the same division a customer deployment of this framework draws for money:
budget arithmetic in code, narrative from the model.

And there is deliberately NO TOTAL. A total needs volumes — tokens per call, calls
per month, GB stored — which are properties of the customer's workload, not of AWS,
and a short request contains none of them. An agent that multiplies a real rate by
an assumed volume has invented the volume, and the invented half is the half that
makes the total wrong.
"""
import asyncio
import json

import pytest

BRIEF = json.dumps({
    "title": "Build an Agentic AI Application",
    "objective": "Design and build an agentic AI application capable of "
                 "autonomous decision-making and task execution.",
})

# Rows as the shipped pricing Lambda returns them.
ROWS = [
    {"service": "Claude Sonnet 5 (Amazon Bedrock Edition)",
     "dimension": "USE1-MP:USE1_input_tokens_standard-Units",
     "unit": "1M tokens", "pricePerUnit": "2.2000000000", "currency": "USD",
     "region": "us-east-1", "text": "rendered for a model"},
    {"service": "Claude Sonnet 5 (Amazon Bedrock Edition)",
     "dimension": "USE1-MP:USE1_output_tokens_standard-Units",
     "unit": "1M tokens", "pricePerUnit": "11.0000000000", "currency": "USD",
     "region": "us-east-1", "text": "..."},
    {"service": "AWS Lambda", "dimension": "Lambda-GB-Second",
     "unit": "Lambda-GB-Second", "pricePerUnit": "0.0000150000",
     "currency": "USD", "region": "us-east-1", "text": "..."},
    {"service": "Amazon Bedrock AgentCore",
     "dimension": "USE1-Runtime:Consumption-based:vCPU", "unit": "vCPU-Hours",
     "pricePerUnit": "0.0895000000", "currency": "USD", "region": "us-east-1",
     "text": "..."},
]
# The mapping workflow.json ships for that Lambda.
FIELDS = {"service": "service", "dimension": "dimension", "unit": "unit",
          "price": "pricePerUnit", "currency": "currency", "region": "region",
          "requested": "requestedAs"}


def _run(rows, *, services='["Amazon Bedrock", "AWS Lambda", "Amazon Bedrock AgentCore"]',
         field_map=None):
    """Run the real agent. Returns (asset, prompts_seen, tool_queries)."""
    from app.common.context import AgentContext
    from app.orchestrator.registry import build_agent_module

    agent = build_agent_module("cost_research")
    ctx = AgentContext(agent, {"topic": "t", "outputs": {"intake": BRIEF},
                               "history": {}},
                       {"configurable": {"thread_id": "s"}})
    prompts: list[str] = []
    queries: list[str] = []

    async def llm(system, user, max_tokens=None, name=None):
        prompts.append(f"{system}\n{user}")
        return json.dumps({"services": json.loads(services),
                           "basis": "an LLM application needs a model, compute "
                                    "and an agent runtime"})

    async def call_tool_rows(tool_key, query=None):
        queries.append(query)
        return rows, (FIELDS if field_map is None else field_map), "gateway"

    ctx.llm = llm
    ctx.call_tool_rows = call_tool_rows
    ctx.log = lambda m: asyncio.sleep(0)
    return json.loads(asyncio.run(agent.run(ctx))), prompts, queries


# ---------------------------------------------------------------------------
# The split
# ---------------------------------------------------------------------------

def test_it_costs_exactly_one_model_call(_=None):
    asset, prompts, _q = _run(ROWS)
    assert len(prompts) == 1, "the service list is the only thing worth asking for"
    assert asset["findings"], "and the prices come from the tool, not the model"


def test_the_model_is_never_shown_a_price(_=None):
    """The heart of it. Every price in the asset appears only AFTER the single model
    call has returned, so there is no opportunity to round, mistype or multiply one."""
    _asset, prompts, _q = _run(ROWS)
    prompt = prompts[0]
    for row in ROWS:
        assert row["pricePerUnit"] not in prompt
        assert row["dimension"] not in prompt
    assert "2.2" not in prompt and "0.0895" not in prompt


def test_the_prompt_asks_for_services_and_forbids_numbers(_=None):
    _asset, prompts, _q = _run(ROWS)
    prompt = prompts[0].lower()
    assert "name the service only" in prompt
    assert "not asked for prices" in prompt


def test_vagueness_is_not_grounds_for_pricing_nothing(_=None):
    """Observed live on the request this workflow demonstrates: given "build an
    agentic AI application" the model returned an empty service list because the
    brief named no domain, and the whole cost step produced nothing.

    The reasoning looks careful and is wrong. WHICH services is answerable from the
    shape of the application; HOW MUCH of them needs volumes, which is exactly why
    this agent never states a total. A published rate does not vary with the domain,
    so domain vagueness cannot make a rate unknowable. The prompt must say that, and
    must reserve the empty answer for a request with nothing runnable in it."""
    from app.subagents.cost_research.prompts import SYSTEM_PROMPT
    prompt = SYSTEM_PROMPT.lower()
    assert "still answerable" in prompt, "vagueness must be addressed head-on"
    assert "does not depend on the domain" in prompt, "and the reason given"
    assert "no runnable workload" in prompt, "empty is for this, and only this"
    assert "too vague" not in prompt, "the old escape hatch must not come back"


def test_the_tool_is_asked_for_the_services_the_model_chose(_=None):
    """Not the run's objective. `call_tool_rows` defaults to the retrieval query,
    which would price nothing here."""
    _asset, _p, queries = _run(ROWS)
    assert queries == ["Amazon Bedrock, AWS Lambda, Amazon Bedrock AgentCore"]


# ---------------------------------------------------------------------------
# Every figure copied, none derived
# ---------------------------------------------------------------------------

def test_every_price_in_the_asset_is_one_the_tool_returned(_=None):
    """A number in a deliverable is read as a commitment, so each one must be
    traceable to a row rather than computed from several."""
    asset, _p, _q = _run(ROWS)
    blob = json.dumps(asset)
    for row in ROWS:
        trimmed = row["pricePerUnit"].rstrip("0").rstrip(".")
        assert trimmed in blob, row["dimension"]


def test_no_total_is_ever_stated(_=None):
    """The rates are real; a total would need volumes nobody supplied. This is the
    invented-figure failure the whole pipeline is built to prevent, so the agent that
    finally has real money in its hands must not be the one to commit it."""
    import re

    asset, _p, _q = _run(ROWS)
    # What the asset ASSERTS — the summary and the findings. Not dataLimitations,
    # where "sessions per month" appears precisely because the agent is naming the
    # volume it does NOT have, which is the behaviour being encouraged.
    asserted = (asset["summary"] + " "
                + " ".join(f["statement"] for f in asset["findings"]))

    # The property, rather than a keyword list: EVERY monetary figure is a RATE, so
    # every one is followed by its unit. A total is a figure with no "per <unit>"
    # after it, which is exactly what makes it read as a commitment.
    for match in re.finditer(r"\d[\d,]*\.?\d*\s*USD", asserted):
        tail = asserted[match.end():match.end() + 6]
        assert tail.startswith(" per "), (
            f"{match.group(0)!r} is stated without a unit, so it reads as a total: "
            f"...{asserted[match.start():match.end() + 40]}...")
    # And no currency symbol at all: the tool reports a currency code per row, so a
    # "$" in the prose came from somewhere the agent made up.
    assert "$" not in asserted


def test_it_names_the_volumes_a_total_would_need(_=None):
    """Naming the gap is the useful half of not filling it: a downstream agent gets
    something concrete to ask for instead of a silence it might fill itself."""
    asset, _p, _q = _run(ROWS)
    limits = " ".join(asset["dataLimitations"]).lower()
    assert "unit prices, not a total" in limits
    assert "tokens" in limits and "per month" in limits
    assert "free-tier" in limits


def test_each_priced_service_gets_a_cited_finding(_=None):
    asset, _p, _q = _run(ROWS)
    for f in asset["findings"]:
        assert f["classification"] == "sourced-fact"
        assert "Price List" in (f["sourceRef"] or "")
    services = {s for row in ROWS for s in [row["service"]]}
    for service in services:
        assert any(service in f["statement"] for f in asset["findings"]), service


def test_the_token_rate_spread_is_a_selection_not_a_calculation(_=None):
    """The cheapest/dearest comparison a reader always wants. Both numbers are quoted
    from rows — no ratio, no multiple, nothing derived."""
    asset, _p, _q = _run(ROWS)
    spread = [f for f in asset["findings"] if "span" in f["statement"]]
    assert spread, "the comparison should be made when models were priced"
    assert "2.2" in spread[0]["statement"] and "11" in spread[0]["statement"]


# ---------------------------------------------------------------------------
# Degrading honestly
# ---------------------------------------------------------------------------

def test_no_prices_returned_says_so_rather_than_implying_free(_=None):
    asset, _p, _q = _run([])
    assert asset["findings"] == []
    assert "no published unit price" in asset["summary"].lower()
    assert asset["dataLimitations"]


def test_an_empty_service_list_does_not_price_a_guessed_stack(_=None):
    """If the model cannot place the request on any service, pricing something
    plausible instead would be a fabrication dressed as evidence."""
    asset, _p, queries = _run(ROWS, services="[]")
    assert queries == [], "the tool must not be called at all"
    assert asset["findings"] == []
    assert "no aws services could be derived" in asset["summary"].lower()


def test_a_wrong_row_mapping_is_reported_with_both_field_sets(_=None):
    """A misconfigured `rowFields` must not look like "these services are free".
    Naming what was looked for and what the rows carry is enough to fix it in
    workflow.json."""
    asset, _p, _q = _run(ROWS, field_map={"service": "svc", "price": "cost"})
    limits = " ".join(asset["dataLimitations"])
    assert "No published price was returned" in limits
    assert "svc" in limits and "pricePerUnit" in limits


def test_a_rate_without_its_unit_is_dropped(_=None):
    """A number with no unit is not a rate a reader can use, and printing one would
    invite them to guess the unit."""
    asset, _p, _q = _run([{**ROWS[0], "unit": ""}])
    assert asset["findings"] == []


# ---------------------------------------------------------------------------
# It is still a normal agent in the pipeline
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("field", ["assetId", "assetType", "summary", "findings",
                                   "dataLimitations", "sources"])
def test_it_emits_the_asset_a_reviewer_expects(field):
    asset, _p, _q = _run(ROWS)
    assert field in asset
    assert asset["assetType"] == "research-finding"


def test_prices_are_printed_readably_without_changing_their_value(_=None):
    """`2.2000000000` reads as noise; `2.2` reads as a price. Only trailing zeros
    go — the value is never rounded."""
    asset, _p, _q = _run(ROWS)
    blob = json.dumps(asset)
    assert "2.2 USD per 1M tokens" in blob
    assert "0.000015 USD per Lambda-GB-Second" in blob


# ---------------------------------------------------------------------------
# Naming what was NOT priced
# ---------------------------------------------------------------------------

def test_a_service_that_came_back_unpriced_is_named(_=None):
    """Live defect: the asset said "Priced 6 of 8" and never said which two. Step
    Functions and SQS were missing, and a reader cannot act on a count. The rows
    carry the caller's own wording so the difference can be named exactly."""
    rows = [dict(r, requestedAs="Amazon Bedrock") for r in ROWS[:2]]
    asset, _p, _q = _run(
        rows, services='["Amazon Bedrock", "AWS Step Functions", "Amazon SQS"]')
    assert "AWS Step Functions" in asset["summary"]
    assert "Amazon SQS" in asset["summary"]
    gap = " ".join(asset["dataLimitations"])
    assert "AWS Step Functions" in gap and "Amazon SQS" in gap


def test_nothing_is_claimed_missing_when_everything_priced(_=None):
    """The mirror case. A clean run must not carry an empty gap sentence."""
    rows = [dict(r, requestedAs="Amazon Bedrock") for r in ROWS]
    asset, _p, _q = _run(rows, services='["Amazon Bedrock"]')
    assert "No rate was returned" not in asset["summary"]
    assert not [x for x in asset["dataLimitations"]
                if "No published unit price was returned for" in x]


# ---------------------------------------------------------------------------
# The volumes a total would need
# ---------------------------------------------------------------------------

def test_the_volumes_named_come_from_the_units_actually_priced(_=None):
    """Live defect: a SERVERLESS DATA PIPELINE was told a total needs "average input
    and output tokens per model call". The list was hardcoded for an LLM workload
    and carried verbatim into the analysis on a run that priced Glue and Kinesis."""
    from app.subagents.cost_research.agent import _volumes_needed
    pipeline = _volumes_needed([{"unit": "DPU-Hour"}, {"unit": "GB-Mo"},
                                {"unit": "Terabytes"}, {"unit": "Requests"}])
    assert not [q for q in pipeline if "token" in q], pipeline
    assert [q for q in pipeline if "scanned" in q]

    agentic = _volumes_needed([{"unit": "1M tokens"}, {"unit": "vCPU-Hours"}])
    assert [q for q in agentic if "token" in q]


def test_each_unit_asks_for_one_volume_not_several(_=None):
    """`Lambda-GB-Second` contains "gb", so scanning every rule against every unit
    asked for data transfer as well as execution time."""
    from app.subagents.cost_research.agent import _volumes_needed
    assert _volumes_needed([{"unit": "Lambda-GB-Second"}]) == [
        "memory size multiplied by execution time, per invocation"]


def test_with_no_rows_the_sentence_is_still_a_sentence(_=None):
    from app.subagents.cost_research.agent import _volumes_needed
    assert len(_volumes_needed([])) == 1


def test_a_tool_that_omits_the_requested_field_claims_nothing(_=None):
    """`requested` is OPTIONAL in `rowFields`. A tool that does not supply it leaves
    every value empty, which would otherwise list every service as unpriced — a
    config omission turned into a false claim about AWS. Found by the test above
    before it could reach a reader."""
    bare = {"service": "service", "dimension": "dimension", "unit": "unit",
            "price": "pricePerUnit", "currency": "currency", "region": "region"}
    asset, _p, _q = _run(ROWS, field_map=bare)
    assert "No rate was returned" not in asset["summary"]
    assert not [x for x in asset["dataLimitations"]
                if "No published unit price was returned for" in x]
    assert asset["findings"], "and the rates themselves still come through"
