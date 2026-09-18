"""The output rules, checked against the output that actually broke them.

Every case below is text a live run really produced (or, for the negative cases,
text a live run really produced that must NOT be flagged). That is the point of
moving these rules out of the prompts: a prompt rule could only be evaluated by
paying for another run and reading it, so it took seven runs to notice that
banning "Week 1" had produced "Phase 1" and then "Priority 1" and then "Step 1 of
7" and then "First… Second… Third…". These run in under a second.

The negative cases matter as much as the positive ones. A check that fires on
correct output costs a wasted repair call and a worse deliverable, so each rule
has at least one case proving it leaves the right behaviour alone.
"""

import pytest

from app.common import rules

# ---------------------------------------------------------------------------
# invented-numbering — the defect that survived five prompt revisions
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("prose,label", [
    # Run 1. Report next-steps, no upstream asset held a single timeline figure.
    (("Immediate (Week 1): confirm the use case. Week 2: select a framework. "
      "Week 3: build the prototype."), "Week"),
    # Run 2, after the prompt banned weeks.
    (("Phase 1 establishes the use case. Phase 2 selects the framework. "
      "Phase 3 hardens for production."), "Phase"),
    # Run 3, after the prompt banned phases.
    (("Priority 1: resolve the open questions. Priority 2: map the maturity "
      "stage. Priority 5: define success metrics."), "Priority"),
    # Run 6, after the prompt banned priority numbering.
    ("Step 1 of 7: choose the LLM. Step 2: design the planning module.", "Step"),
])
def test_each_renaming_of_the_numbered_plan_is_caught(prose, label):
    found = rules.check({"summary": prose}, upstream="high, medium, low",
                        rules=rules.SYNTHESIS)
    numbering = [v for v in found if v.rule == "invented-numbering"]
    assert numbering, f"{label} series was not caught"
    assert label.lower() in numbering[0].detail.lower()


def test_the_ordinal_word_sequence_is_the_same_defect():
    """Run 7. No noun to ban, so the denylist approach had nothing to match."""
    prose = ("First, map the use case to the maturity stage. Second, define the "
             "core components. Third, design the guardrails. Fourth, specify tool "
             "integration. Fifth, design state management. Sixth, plan "
             "observability.")
    found = rules.check({"sections": [{"sectionType": "next-steps", "content": prose}]},
                        upstream="", rules=rules.SYNTHESIS)
    assert [v for v in found if v.rule == "invented-numbering"]


def test_a_series_the_inputs_really_define_is_allowed():
    """The whole reason this is a grounding test and not a denylist.

    AWS documentation defines four named stages; a report that carries them is
    reporting its evidence, not inventing a plan. Same noun, same shape as the
    banned case — only the grounding differs.
    """
    upstream = ("Stage 1: generative AI assistants. Stage 2: generative AI agents "
                "with guardrails. Stage 3: agentic AI systems. Stage 4: autonomous "
                "multi-agent systems.")
    prose = "The use case maps to Stage 2, and Stage 3 once oversight is defined."
    found = rules.check({"summary": prose}, upstream=upstream, rules=rules.SYNTHESIS)
    assert not [v for v in found if v.rule == "invented-numbering"]


def test_a_partially_grounded_series_is_caught_on_the_invented_members():
    """Quoting Stage 1-4 does not license a Stage 7 that no input mentions."""
    found = rules.check({"summary": "Move from Stage 2 to Stage 7."},
                        upstream="Stage 1 Stage 2 Stage 3 Stage 4",
                        rules=rules.SYNTHESIS)
    detail = " ".join(v.detail for v in found if v.rule == "invented-numbering")
    assert "Stage 7" in detail
    assert "Stage 2" not in detail


def test_summarising_your_own_output_as_a_numbered_approach_is_caught():
    found = rules.check({"executiveSummary": "We recommend a three-phase approach."},
                        upstream="no phases here", rules=rules.SYNTHESIS)
    assert [v for v in found if v.rule == "invented-numbering"]


def test_a_compound_count_the_evidence_stated_is_allowed():
    found = rules.check({"summary": "Building such a system follows seven steps."},
                        upstream="Building an agentic system from scratch follows "
                                 "seven steps: 1. Choose your LLM",
                        rules=rules.SYNTHESIS)
    assert not [v for v in found if v.rule == "invented-numbering"]


def test_one_ordinal_mention_is_not_a_series():
    """'the first stage' is a reference to the inputs, not a plan of its own."""
    found = rules.check({"summary": "Phase 1 of the AWS model is the assistant stage."},
                        upstream="", rules=rules.SYNTHESIS)
    assert not [v for v in found if v.rule == "invented-numbering"]


def test_plain_enumeration_of_an_upstream_list_is_left_alone():
    """Numbering a list the inputs already contain adds no false commitment, and
    flagging it would make the check noisy enough that someone turns it off."""
    prose = ("Core components: 1. Reasoning engine. 2. Planning module. "
             "3. Tool integration. 4. Memory. 5. Orchestration.")
    found = rules.check({"summary": prose}, upstream="reasoning engine, planning "
                        "module, tool integration, memory, orchestration",
                        rules=rules.SYNTHESIS)
    assert not [v for v in found if v.rule == "invented-numbering"]


# ---------------------------------------------------------------------------
# unsupported-figure
# ---------------------------------------------------------------------------

def test_an_illustrative_figure_is_still_an_invented_figure():
    """Observed as 'e.g. approve expenses under $5,000'. Once it is in a report a
    reader cannot tell an illustration from a requirement."""
    found = rules.check({"items": [{"title": "Set thresholds",
                                    "detail": "e.g. approve expenses under $5,000"}]},
                        upstream="no thresholds were supplied", rules=rules.SYNTHESIS)
    assert [v for v in found if v.rule == "unsupported-figure"]


@pytest.mark.parametrize("figure", ["6-12 months", "80%", "$5,000", "1,200 users"])
def test_figures_of_every_shape_are_checked(figure):
    found = rules.check({"summary": f"Expect {figure} for delivery."},
                        upstream="", rules=rules.SYNTHESIS)
    assert [v for v in found if v.rule == "unsupported-figure"], figure


def test_a_figure_the_evidence_supplied_is_allowed():
    """Run 7's '85% of routine tasks' came from a retrieved article and is a
    legitimate, attributed citation."""
    found = rules.check(
        {"summary": "Agentic workflows handle approximately 85% of routine tasks."},
        upstream="According to McKinsey research, agentic workflows now handle "
                 "approximately 85% of routine tasks autonomously.",
        rules=rules.SYNTHESIS)
    assert not [v for v in found if v.rule == "unsupported-figure"]


def test_thousands_separators_do_not_defeat_the_grounding_check():
    found = rules.check({"summary": "The limit is $5000."},
                        upstream="a hard limit of $5,000 per request",
                        rules=rules.SYNTHESIS)
    assert not [v for v in found if v.rule == "unsupported-figure"]


def test_digits_inside_an_asset_id_or_a_url_are_not_figures():
    """Every asset carries ids and citations full of digits; scanning them would
    bury the one figure that matters in forty that do not."""
    payload = {"assetId": "asset-analysis-agentic-ai-v2",
               "sources": [{"sourceType": "web article", "sourceName": "Guide 2026",
                            "url": "https://example.com/a/2b2aa6d16118"}],
               "summary": "See the retrieved guide."}
    found = rules.check(payload, upstream="", rules=rules.SYNTHESIS)
    assert not [v for v in found if v.rule == "unsupported-figure"]


# ---------------------------------------------------------------------------
# about-the-request
# ---------------------------------------------------------------------------

def test_the_systems_own_run_history_is_not_content():
    """Run 7: the analysis promoted this to a traced claim and the recommendation
    carried it into its risks."""
    payload = {"claims": [{"statement": "Six prior runs on agentic AI application "
                                        "development have all completed successfully."}]}
    found = rules.check(payload, upstream="", rules=rules.SYNTHESIS)
    assert [v for v in found if v.rule == "about-the-request"]


def test_an_invented_fact_about_the_requester_is_caught():
    """Observed as the stated rationale for recommending Lambda, for a request
    whose entire text was five words."""
    payload = {"items": [{"title": "Use Lambda",
                          "rationale": "The user's background is hands-on with AWS "
                                       "serverless, so Lambda is feasible."}]}
    found = rules.check(payload, upstream="", rules=rules.SYNTHESIS)
    assert [v for v in found if v.rule == "about-the-request"]


def test_a_label_the_inputs_wrote_closed_up_is_still_grounded():
    """Live false positive from the first run that priced anything. The AWS Price
    List returns usage types closed up — `Requests-Tier1`, `Requests-Tier2` — and
    an analysis quoting them as "Tier 1" and "Tier 2" was reported for inventing a
    series its own evidence had handed it. The separator carries no meaning, and
    _SERIES_RE already ignores it when detecting a label; the lookup has to agree."""
    upstream = ("Amazon Simple Storage Service on-demand rates: Requests-Tier1 at "
                "0.000005 USD per Requests; Requests-Tier2 at 0.0000004 USD.")
    payload = {"summary": "Amazon S3 rates are 0.000005 USD per request (Tier 1), "
                          "and 0.0000004 USD per request (Tier 2)."}
    assert rules.check(payload, upstream=upstream, rules=rules.SYNTHESIS) == []


@pytest.mark.parametrize("upstream", [
    "Tier-1 and Tier-2 are defined",
    "Tier 1 and Tier 2 are defined",
    "Tier1 and Tier2 are defined",
])
def test_the_separator_is_ignored_in_both_directions(upstream):
    """Whichever way round the two sides write it."""
    assert not [v for v in rules.check({"summary": "Use Tier 1, then Tier 2."},
                                       upstream=upstream, rules=rules.SYNTHESIS)
                if v.rule == "invented-numbering"]


def test_an_invented_series_still_fires_after_the_tolerance():
    """The tolerance must not swallow the rule it belongs to."""
    found = rules.check({"summary": "Deliver in Phase 1, then Phase 2, then Phase 3."},
                        upstream="no phases anywhere", rules=rules.SYNTHESIS)
    assert [v for v in found if v.rule == "invented-numbering"]


def test_run_history_is_allowed_where_it_is_the_subject():
    """The rule is off in the RESEARCH profile. A research agent reports whatever
    its source returned, and if that source is the system's own run table then run
    history IS the subject. The rule exists to stop SYNTHESIS agents promoting it
    to a finding about the request; firing it upstream would gag the source."""
    payload = {"findings": [{"statement": "Run 8a4bacc44a8b completed on this topic.",
                             "classification": "sourced-fact"}]}
    assert not rules.check(payload, upstream="Run 8a4bacc44a8b - outcome: done",
                           rules=rules.RESEARCH)


def test_the_word_user_in_ordinary_prose_survives():
    found = rules.check({"summary": "Each user session needs its own namespace."},
                        upstream="", rules=rules.SYNTHESIS)
    assert not [v for v in found if v.rule == "about-the-request"]


# ---------------------------------------------------------------------------
# finding-about-the-brief
# ---------------------------------------------------------------------------

def test_restating_the_brief_as_a_finding_is_caught():
    """Two runs in a row, in three different agents. Every downstream reader
    already has the brief, so the slot is wasted."""
    payload = {"findings": [
        {"statement": "The current request brief identifies six open questions "
                      "that remain unanswered.", "classification": "sourced-fact"}]}
    found = rules.check(payload, upstream="", rules=rules.RESEARCH)
    assert [v for v in found if v.rule == "finding-about-the-brief"]


def test_naming_a_brief_gap_in_the_limitations_is_the_correct_behaviour():
    """dataLimitations is where 'the brief does not say' belongs, so the rule
    must not chase it there — otherwise it punishes the fix it is asking for."""
    payload = {"dataLimitations": ["The request brief does not name a target domain."],
               "findings": [{"statement": "Agentic systems need durable state.",
                             "classification": "sourced-fact"}]}
    assert not [v for v in rules.check(payload, upstream="durable state",
                                       rules=rules.RESEARCH)
                if v.rule == "finding-about-the-brief"]


# ---------------------------------------------------------------------------
# count-mismatch
# ---------------------------------------------------------------------------

def test_a_count_that_does_not_match_its_list_is_caught():
    """Observed: 'seven open questions', then six of them."""
    payload = {"summary": "There are seven open questions.",
               "openQuestions": ["a", "b", "c", "d", "e", "f"]}
    found = rules.check(payload, upstream="", rules=rules.BRIEF)
    assert [v for v in found if v.rule == "count-mismatch"]


def test_a_count_is_checked_against_the_brief_when_the_payload_has_no_copy():
    """A synthesis agent counting the brief's open questions holds none of them,
    so without the caller passing them in the count is unresolvable."""
    payload = {"summary": "The brief leaves seven critical open questions."}
    found = rules.check(payload, upstream="", rules=rules.SYNTHESIS,
                        extra_counts={"openQuestions": 6})
    assert [v for v in found if v.rule == "count-mismatch"]


def test_a_correct_count_passes():
    payload = {"summary": "Six critical open questions remain."}
    found = rules.check(payload, upstream="", rules=rules.SYNTHESIS,
                        extra_counts={"openQuestions": 6})
    assert not [v for v in found if v.rule == "count-mismatch"]


def test_an_unresolvable_count_is_left_alone_rather_than_guessed():
    """The report holds no openQuestions list and was given no count, so there is
    nothing to compare against. Silence beats a false positive."""
    payload = {"sections": [{"sectionType": "analysis",
                             "content": "Six critical open questions remain."}]}
    assert not [v for v in rules.check(payload, upstream="", rules=rules.SYNTHESIS)
                if v.rule == "count-mismatch"]


# ---------------------------------------------------------------------------
# duplicate-in-list
# ---------------------------------------------------------------------------

def test_the_same_limitation_twice_is_caught():
    payload = {"limitations": [
        "No evidence was retrieved on cost-benefit analysis for agentic projects.",
        "No evidence was retrieved on cost benefit analysis for agentic projects.",
        "The knowledge base holds no domain-specific use-case templates."]}
    found = rules.check(payload, upstream="", rules=rules.SYNTHESIS)
    assert len([v for v in found if v.rule == "duplicate-in-list"]) == 1


def test_distinct_entries_are_not_merged():
    payload = {"risks": ["Guardrails that are too restrictive cripple autonomy.",
                         ("Prompt injection from retrieved tool output is a "
                          "documented failure mode.")]}
    assert not [v for v in rules.check(payload, upstream="", rules=rules.SYNTHESIS)
                if v.rule == "duplicate-in-list"]



NINE_BUT_EIGHT = (
    "Production-grade agentic workflows require nine core best practices: "
    "tool-first design over Model Context Protocol (MCP), pure-function "
    "invocation, single-tool and single-responsibility agents, externalized "
    "prompt management, responsible-AI-aligned model-consortium design, clean "
    "separation between workflow logic and MCP servers, containerized deployment, "
    "and adherence to the KISS principle.")


# ---------------------------------------------------------------------------
# finding-about-the-brief, in the summary fields
# ---------------------------------------------------------------------------

def test_a_summary_about_the_request_is_the_same_defect_as_a_finding():
    """Guarding only findings/claims left the one line an approver is guaranteed to
    read unguarded. An analysis and a recommendation both opened with this."""
    payload = {"executiveSummary": "Agentic AI systems require four components. "
                                   "However, the request brief lacks critical "
                                   "specificity on use case and autonomy level."}
    found = rules.check(payload, upstream="", rules=rules.SYNTHESIS)
    assert [v for v in found if v.rule == "finding-about-the-brief"]


def test_the_brief_as_a_modifier_is_not_the_subject():
    """Tightening to the subject position, because matching the brief anywhere in a
    summary flagged this: the sentence is about the evidence and names the brief
    only to say what the evidence covers. Padding at worst, not this defect."""
    payload = {"summary": "Web evidence provides current architectural patterns, "
                          "component frameworks and implementation approaches that "
                          "address the brief's scope of architecture, agent "
                          "capabilities, tool integration and deployment."}
    assert not [v for v in rules.check(payload, upstream="", rules=rules.SYNTHESIS)
                if v.rule == "finding-about-the-brief"]


def test_a_gap_in_the_limitations_list_is_still_the_right_place():
    """The rule must not chase the substance out of the field that is FOR it."""
    payload = {"summary": "Serverless suits agentic workloads.",
               "dataLimitations": ["The request brief does not specify a domain."],
               "limitations": ["The brief leaves the autonomy level open."]}
    assert not [v for v in rules.check(payload, upstream="", rules=rules.SYNTHESIS)
                if v.rule == "finding-about-the-brief"]


# ---------------------------------------------------------------------------
# action-on-unavailable
# ---------------------------------------------------------------------------

def test_the_blunt_form_is_caught():
    payload = {"items": [{"title": "Reuse prior work",
                          "detail": "Consult the artifacts from the four prior runs.",
                          "rationale": "Those artifacts are not available."}]}
    found = rules.check(payload, upstream="", rules=rules.SYNTHESIS)
    assert [v for v in found if v.rule == "action-on-unavailable"]


def test_the_hedged_form_is_the_same_defect():
    """This is what the output produced AFTER the prompt banned the blunt form:
    the gap named as both a step and its own caveat, inside one item."""
    payload = {"sections": [{"sectionType": "recommendations",
                             "content": "Attempt to retrieve prior run artifacts, "
                                        "which may not be retrievable."}]}
    found = rules.check(payload, upstream="", rules=rules.SYNTHESIS)
    assert [v for v in found if v.rule == "action-on-unavailable"]


def test_stating_a_gap_in_the_limitations_is_not_an_action():
    """The rule is scoped to action-bearing fields precisely so that the honest
    behaviour — naming what is missing — is never penalised."""
    payload = {"limitations": [("Prior run artifacts are not available, so their "
                               "reuse cannot be evaluated.")],
               "risks": ["Prior run outputs are not available for review."]}
    assert not [v for v in rules.check(payload, upstream="", rules=rules.SYNTHESIS)
                if v.rule == "action-on-unavailable"]


def test_a_narrative_section_is_not_an_action_list():
    payload = {"sections": [{"sectionType": "background",
                             "content": "Reviewing the literature is hard because "
                                        "vendor benchmarks are not available."}]}
    assert not [v for v in rules.check(payload, upstream="", rules=rules.SYNTHESIS)
                if v.rule == "action-on-unavailable"]


# ---------------------------------------------------------------------------
# The contract of check() itself
# ---------------------------------------------------------------------------

def test_an_empty_or_unparseable_payload_has_no_violations():
    """The runners already degrade on an empty payload; this layer must not turn
    that into a second, different failure."""
    assert rules.check({}, upstream="x") == []
    assert rules.check(None, upstream="x") == []  # type: ignore[arg-type]


def test_check_never_mutates_the_payload():
    payload = {"summary": "Phase 1 then Phase 2.", "limitations": ["a"]}
    before = repr(payload)
    rules.check(payload, upstream="", rules=rules.SYNTHESIS)
    assert repr(payload) == before


def test_profiles_disable_the_rules_that_do_not_apply():
    assert "about-the-request" not in rules.RESEARCH
    assert "action-on-unavailable" not in rules.BRIEF
    assert "finding-about-the-brief" not in rules.BRIEF
    assert rules.SYNTHESIS == rules.ALL_RULES


def test_the_repair_block_quotes_the_offence_and_caps_the_examples():
    """A generic restatement of the rule is what the system prompt already said
    and the model already ignored, so the re-ask has to be specific — but a wall
    of forty examples makes the instruction longer than the task."""
    found = rules.check(
        {"summary": " ".join(f"Budget {n}00 dollars." for n in range(1, 9))},
        upstream="", rules=rules.SYNTHESIS)
    text = rules.as_repair_instructions(found)
    assert "100" in text
    assert "further unsupported-figure problems" in text
    assert text.count("\n- ") <= 7


def test_violation_renders_as_rule_and_detail():
    v = rules.Violation("invented-numbering", "you wrote Phase 1")
    assert str(v) == "invented-numbering: you wrote Phase 1"


# ---------------------------------------------------------------------------
# structured.ask_json — the bounded repair loop
# ---------------------------------------------------------------------------
#
# The loop is bounded at one retry on purpose. An unbounded loop against a
# sampler can spend a whole token budget converging on nothing, and this
# framework's existing convention for "I could not establish that" is to emit a
# VALID asset that names its own shortfall — not to fail, and not to retry until
# the budget runs out.


def _run(coro):
    """Drive one coroutine to completion.

    The rest of this suite is synchronous and the repo carries no async test
    plugin; adding one for seven cases would be a dependency for nothing.
    """
    import asyncio

    return asyncio.run(coro)


class _FakeCtx:
    """Enough of AgentContext for ask_json: llm(), log(), agent_id, output_rules.

    `repair` defaults to True HERE, in the fixture for the repair tests, and to
    False in the shipped config. That inversion is deliberate: these tests are
    about what the repair loop does when a customer has asked for it, and the
    default must not be silently exercised by a test that thinks it is testing
    something else. The default itself is pinned by
    test_repair_is_off_unless_the_customer_asks_for_it.
    """

    def __init__(self, *answers: str, enabled: bool = True, repair: bool = True,
                 max_words: int = 0):
        self.answers = list(answers)
        self.agent_id = "analysis"
        self.calls: list[str] = []
        self.logs: list[str] = []
        # `maxWords` arrives the same way every other setting does — through
        # output_rules, from config.output_rules_for. ask_json takes no budget
        # argument, so a test cannot set it in a way production could not.
        self.output_rules = {"enabled": enabled, "repair": repair,
                             "maxWords": max_words}

    async def llm(self, system, user, max_tokens=None, name=None):
        self.calls.append(user)
        return self.answers.pop(0) if self.answers else "{}"

    async def log(self, message):
        self.logs.append(message)


@pytest.fixture()
def structured():
    from app.common import structured as s
    return s


def test_clean_output_costs_exactly_one_model_call(structured):
    ctx = _FakeCtx('{"summary": "Grounded prose with no figures."}')
    payload, unrepaired = _run(structured.ask_json(
        ctx, "sys", "user", rule_set=rules.SYNTHESIS, upstream=""))
    assert payload == {"summary": "Grounded prose with no figures."}
    assert unrepaired == []
    assert len(ctx.calls) == 1


def test_a_violation_triggers_one_re_ask_carrying_the_detail(structured):
    ctx = _FakeCtx('{"summary": "Phase 1 then Phase 2."}',
                   '{"summary": "Settle the use case before choosing a framework."}')
    payload, unrepaired = _run(structured.ask_json(
        ctx, "sys", "user", rule_set=rules.SYNTHESIS, upstream=""))
    assert len(ctx.calls) == 2
    assert "Phase 1" in ctx.calls[1], "the re-ask must quote the offending text"
    assert "BROKE THESE RULES" in ctx.calls[1]
    assert payload["summary"].startswith("Settle the use case")
    assert unrepaired == []


def test_the_loop_stops_at_one_retry(structured):
    """Three bad answers available; only two calls are ever made."""
    bad = '{"summary": "Phase 1 then Phase 2."}'
    ctx = _FakeCtx(bad, bad, bad)
    _, unrepaired = _run(structured.ask_json(
        ctx, "sys", "user", rule_set=rules.SYNTHESIS, upstream=""))
    assert len(ctx.calls) == 2
    assert unrepaired, "what could not be repaired must be reported, not swallowed"


def test_an_unimproved_retry_keeps_the_original_answer(structured):
    """A rewrite that fixes nothing has usually also lost content, so the first
    answer plus an honest note beats the second answer."""
    ctx = _FakeCtx('{"summary": "Phase 1 then Phase 2.", "limitations": ["real gap"]}',
                   '{"summary": "Phase 1 then Phase 2 then Phase 3."}')
    payload, unrepaired = _run(structured.ask_json(
        ctx, "sys", "user", rule_set=rules.SYNTHESIS, upstream=""))
    assert payload["limitations"] == ["real gap"]
    assert len(unrepaired) == 1


def test_a_partial_repair_is_kept_and_the_rest_reported(structured):
    ctx = _FakeCtx('{"summary": "Phase 1 then Phase 2, budget $5,000."}',
                   '{"summary": "Order by dependency, budget $5,000."}')
    payload, unrepaired = _run(structured.ask_json(
        ctx, "sys", "user", rule_set=rules.SYNTHESIS, upstream=""))
    assert "Phase" not in payload["summary"]
    assert len(unrepaired) == 1
    assert "unsupported-figure" in unrepaired[0]


def test_an_unparseable_answer_degrades_exactly_as_before(structured):
    """No payload means no rules to check — the runners' existing degrade path
    owns this case, and this layer must not invent a second one."""
    ctx = _FakeCtx("the model apologised in prose")
    payload, unrepaired = _run(structured.ask_json(
        ctx, "sys", "user", rule_set=rules.SYNTHESIS, upstream=""))
    assert payload == {}
    assert unrepaired == []
    assert len(ctx.calls) == 1


def test_an_unparseable_repair_falls_back_to_the_first_answer(structured):
    ctx = _FakeCtx('{"summary": "Phase 1 then Phase 2."}', "sorry, no JSON")
    payload, unrepaired = _run(structured.ask_json(
        ctx, "sys", "user", rule_set=rules.SYNTHESIS, upstream=""))
    assert payload["summary"] == "Phase 1 then Phase 2."
    assert unrepaired


# ---------------------------------------------------------------------------
# What enforcement COSTS is the customer's decision, not the framework's
# ---------------------------------------------------------------------------
#
# The repair loop shipped unconditionally in its first version. On its first live
# run seven of eight agents failed a check and were re-asked, so a run cost very
# nearly twice what the same run cost before the rules existed — a framework
# quietly doubling a customer's bill to improve its own output quality. The checks
# are free; only the re-ask is not. These tests pin that separation.

def test_repair_is_off_unless_the_customer_asks_for_it(structured):
    """The DEFAULT. A failed check records the violation and stops there: one call,
    the same cost as no enforcement at all, and the reviewer still sees exactly
    what was wrong because it lands on the asset (and in the UI above the summary).
    """
    ctx = _FakeCtx('{"summary": "Phase 1 then Phase 2, budget $5,000."}',
                   '{"summary": "a repair that must never be requested"}',
                   repair=False)
    payload, unrepaired = _run(structured.ask_json(
        ctx, "sys", "user", rule_set=rules.SYNTHESIS, upstream=""))

    assert len(ctx.calls) == 1, "check-only must not spend a second model call"
    assert payload["summary"].startswith("Phase 1")   # untouched
    assert len(unrepaired) == 2                       # numbering + figure
    assert any("invented-numbering" in v for v in unrepaired)
    assert any("unsupported-figure" in v for v in unrepaired)


def test_the_shipped_default_does_not_pay_for_repair():
    """Pinned against workflow.json itself, not against a fixture. If someone flips
    the shipped default to repair-on, that is a change to what every customer's run
    costs and it should have to fail a test to happen."""
    from app.common.config import OUTPUT_RULES

    assert OUTPUT_RULES["enabled"] is True, "checks are free; they should be on"
    assert OUTPUT_RULES["repair"] is False, (
        "repair costs a second model call per failing agent. Shipping it on by "
        "default silently doubles a customer's bill.")


def test_disabling_the_rules_entirely_skips_the_checks(structured):
    """`enabled: false` is a real off switch: no violations recorded, nothing
    re-asked, output passed through exactly as the model produced it."""
    ctx = _FakeCtx('{"summary": "Phase 1 then Phase 2, budget $5,000."}',
                   enabled=False)
    payload, unrepaired = _run(structured.ask_json(
        ctx, "sys", "user", rule_set=rules.SYNTHESIS, upstream=""))

    assert len(ctx.calls) == 1
    assert unrepaired == []
    assert payload["summary"].startswith("Phase 1")


def test_repair_on_costs_a_second_call_only_for_an_agent_that_failed(structured):
    """The opt-in is proportional: a clean agent still costs one call."""
    clean = _FakeCtx('{"summary": "Grounded prose, no figures."}', repair=True)
    _run(structured.ask_json(clean, "sys", "user", rule_set=rules.SYNTHESIS,
                             upstream=""))
    assert len(clean.calls) == 1

    failed = _FakeCtx('{"summary": "Phase 1 then Phase 2."}',
                      '{"summary": "Ordered by dependency."}', repair=True)
    _run(structured.ask_json(failed, "sys", "user", rule_set=rules.SYNTHESIS,
                             upstream=""))
    assert len(failed.calls) == 2


def test_a_repair_that_trades_one_defect_for_another_is_rejected(structured):
    """Counting alone is not enough. This rewrite drops two ungrounded figures and
    invents a numbered plan instead — a lower count, and not an improvement. The
    reviewer was told about the figures; nobody asked for a schedule."""
    ctx = _FakeCtx('{"summary": "Budget $5,000 over 6 months and 30 days."}',
                   '{"summary": "Phase 1 discovery, then Phase 2 delivery."}',
                   repair=True)
    payload, unrepaired = _run(structured.ask_json(
        ctx, "sys", "user", rule_set=rules.SYNTHESIS, upstream=""))

    assert payload["summary"].startswith("Budget"), "the original must be kept"
    assert all("invented-numbering" not in v for v in unrepaired)
    assert any("re-ask introduced invented-numbering" in log for log in ctx.logs)


def test_a_per_agent_override_can_turn_repair_on_for_one_agent_only():
    """A customer should be able to pay for the report and not for the four
    research agents. The override merges over the engine default, so an agent that
    only wants repair writes {"repair": true} and nothing else."""
    from app.common.config import OUTPUT_RULES, output_rules_for

    assert output_rules_for({}) == OUTPUT_RULES
    assert output_rules_for({"outputRules": {"repair": True}}) == {
        **OUTPUT_RULES, "repair": True}
    assert output_rules_for({"outputRules": {"enabled": False}})["enabled"] is False


def test_the_setting_reaches_every_agent_object():
    """Config that does not arrive on the Agent is decoration. Same reason
    test_config_keys pins maxTokens.

    Checked against the PER-AGENT merge rather than the engine default. No agent
    overrides it today — one did briefly, and the measurement said not to (see
    app/subagents/analysis/prompts.py) — but comparing against the default would stop
    testing the path an override travels on, and would fail the moment somebody adds
    one correctly. The synthetic case below covers the merge itself.
    """
    import json

    from conftest import ORCH_ROOT

    from app.common.config import output_rules_for
    from app.orchestrator.registry import load_agents

    specs = json.loads((ORCH_ROOT / "app" / "workflow.json").read_text())["agents"]
    loaded = load_agents()
    assert set(loaded) == set(specs), "every configured agent must load"
    for agent_id, agent in loaded.items():
        assert agent.output_rules == output_rules_for(specs[agent_id]), agent_id
    # The merge must actually merge, whether or not the shipped config uses it.
    assert output_rules_for({"outputRules": {"repair": True}})["repair"] is True
    assert output_rules_for({})["repair"] is False


# ---------------------------------------------------------------------------
# The prompts must give a legal home to what the checks forbid
# ---------------------------------------------------------------------------
#
# `finding-about-the-brief` fired in all four research agents and in the analysis
# agent on the same run, and the cause was not the model ignoring an instruction.
# It was two of my own instructions contradicting each other: rule 3 sent a gap in
# the request to `dataLimitations`, and rule 4 then said `dataLimitations` was for
# "ONLY genuinely missing evidence". The model had an observation it judged
# important and no legal place to put it, so it put it in findings.
#
# A check that forbids something the prompt gives nowhere else to go is a check
# that guarantees a repair call on every run. These tests pin the routing.

def test_research_instructions_route_a_request_gap_to_data_limitations():
    from app.common.research import RESEARCH_INSTRUCTIONS as text

    assert "dataLimitations is the home for BOTH kinds of gap" in text, (
        "the list that receives request-gaps must say it receives them")
    assert "ONLY genuinely missing evidence" not in text, (
        "this is the contradiction: it sent request-gaps to dataLimitations and "
        "then narrowed dataLimitations to evidence-gaps, leaving the model no "
        "legal home for the observation")


def test_research_instructions_show_the_shape_being_forbidden():
    """Abstract prohibitions lost five rounds to synonyms. What worked was quoting
    the offending sentence back, which is what the repair instruction does — so the
    first-pass prompt should do it too, and save the repair call."""
    from app.common.research import RESEARCH_INSTRUCTIONS as text

    assert "The request brief identifies" in text
    assert "NOT" in text and "BUT" in text, "show the rewrite, not just the ban"


def test_synthesis_rules_name_limitations_as_the_home_for_a_request_gap():
    """Same fix on the synthesis side, where four analysis claims were flagged."""
    import inspect

    from app.common import synthesis

    text = inspect.getsource(synthesis.synthesize)
    assert "limitations" in text and "never a claim" in text
    assert "The request brief identifies" in text, (
        "quote the shape; 'write about the subject' alone did not hold")


def test_synthesis_rules_still_forbid_run_history_as_content():
    """The other dominant true positive: prior-run material carried into a
    deliverable as a claim or a report section."""
    import inspect

    from app.common import synthesis

    text = inspect.getsource(synthesis.synthesize)
    assert "prior runs completed" in text.lower()
    assert "run history" in text


# ---------------------------------------------------------------------------
# A label's digits are not a quantity; a decomposition counted is not invented
# ---------------------------------------------------------------------------

def test_a_tier_label_is_not_a_count_of_requests():
    """Live false positive. "0.000005 USD per Tier-1 Request, 0.0000004 USD per
    Tier-2 Request" reported "1 Request" and "2 Request" as ungrounded figures: the
    hyphen in `Tier-1` opens a word boundary and `request` is a unit, so a tier label
    was read as a count. The upstream said `Requests-Tier1`, so they were grounded
    too — nothing about them was a quantity."""
    upstream = ("Amazon Simple Storage Service: Requests-Tier1 at 0.000005 USD per "
                "Requests; Requests-Tier2 at 0.0000004 USD per Requests; "
                "TimedStorage-ByteHrs at 0.022 USD per GB-Mo.")
    payload = {"summary": "S3 charges 0.000005 USD per Tier-1 Request, 0.0000004 USD "
                          "per Tier-2 Request, and 0.022 USD per GB-Mo."}
    assert rules.check(payload, upstream=upstream, rules=rules.SYNTHESIS) == []


@pytest.mark.parametrize("text,figure", [
    ("Set alarms on failure rate threshold (e.g., >1% Lambda errors).", "1%"),
    ("Alert when processing lag exceeds 1 hour behind real-time.", "1 hour"),
    ("Right-size memory allocation (128 MB to 10 GB) based on workload.", "128 MB"),
])
def test_the_label_tolerance_does_not_excuse_an_invented_threshold(text, figure):
    """The invented thresholds from the same live run, all hedged with "e.g." —
    which changes nothing, because an approver reads the number and not the hedge.
    The tolerance is for label digits only."""
    details = " ".join(v.detail for v in
                       rules.check({"summary": text}, upstream="nothing here",
                                   rules=rules.SYNTHESIS)
                       if v.rule == "unsupported-figure")
    assert f'"{figure}"' in details, text


@pytest.mark.parametrize("text,label", [
    ("S3 charges 0.000005 USD per Tier 1 Request.", "1 Request"),
    ("Glue charges 0.308 USD per Gen-2 DPU-Hour.", "2 DPU-Hour"),
    ("Athena charges 5 USD per Tier-1 Terabyte.", "1 Terabyte"),
])
def test_only_the_label_digits_are_excused_not_the_price(text, label):
    """The USD amount in each of these is genuinely ungrounded here and SHOULD be
    reported. What must not be reported is the label — the tolerance is narrow, and a
    test asserting the whole sentence comes back clean would have hidden that."""
    details = " ".join(v.detail for v in
                       rules.check({"summary": text}, upstream="nothing here",
                                   rules=rules.SYNTHESIS)
                       if v.rule == "unsupported-figure")
    assert f'"{label}"' not in details, f"label reported as a quantity: {text}"
    assert "USD" in details, "the price itself is still ungrounded and must be named"


def test_a_compound_count_is_grounded_by_a_synonym_decomposition():
    """Live false positive: the report wrote "layers data flow across four stages"
    where every upstream asset says "four-layer" and names all four. The count is the
    evidence's; only the noun is the model's. Same family as the "four stages" case
    already adjudicated FALSE in the 18:22 corpus."""
    upstream = ("The evidence converges on a four-layer design: ingestion via "
                "purpose-built services, processing, storage, and consumption.")
    payload = {"summary": "The architecture layers data flow across four stages."}
    assert not [v for v in rules.check(payload, upstream=upstream,
                                       rules=rules.SYNTHESIS)
                if v.rule == "invented-numbering"]


def test_a_compound_count_is_grounded_by_a_plain_enumeration_upstream():
    """Live false positive on "build analytics applicaiton on AWS".

    `documentation_search` wrote "a consistent four-stage pipeline: collect, store,
    process, and analyze/visualize" and was told it invented the structure. Its
    evidence block contained the list verbatim, from an AWS page. The two existing
    escapes both miss this shape: there are no `Stage 1 … Stage 4` labels for
    `_upstream_series_size`, and the words "four stages" never appear anywhere for
    `_COMPOUND_GROUND_RE` — the source states the count only by enumerating it.
    """
    upstream = ("A typical analytics pipeline has the following stages:\n"
                "1. Collect data\n2. Store the data\n3. Process the data\n"
                "4. Analyze and visualize the data")
    payload = {"summary": "AWS analytics follows a consistent four-stage pipeline: "
                          "collect, store, process, and analyze/visualize."}
    assert not [v for v in rules.check(payload, upstream=upstream,
                                       rules=rules.RESEARCH)
                if v.rule == "invented-numbering"]


def test_a_plain_enumeration_grounds_only_the_count_it_actually_lists():
    """The escape must not become a blanket pass for any number.

    Grounding "four stages" off a four-item list is right; grounding "six stages"
    off the same list is the failure this whole rule exists to catch.
    """
    upstream = ("A typical analytics pipeline has the following stages:\n"
                "1. Collect data\n2. Store the data\n3. Process the data\n"
                "4. Analyze and visualize the data")
    assert [v for v in rules.check({"summary": "It follows a six-stage pipeline."},
                                   upstream=upstream, rules=rules.RESEARCH)
            if v.rule == "invented-numbering"]


def test_a_separate_numbered_list_does_not_extend_an_earlier_one():
    """Why the walk stops at the first mismatch instead of skipping it.

    A research prompt's evidence block carries several numbered lists, including the
    instruction list appended to every research call. A permissive walk read the
    four-item list above as NINE by hopping into the next list, which would have
    grounded "nine stages" that nothing defines. Measured on the live upstream.
    """
    upstream = ("The following stages apply:\n1. Collect\n2. Store\n3. Process\n"
                "4. Analyze\n\n=== HOW TO USE THESE INPUTS ===\n"
                "1. The REQUEST is authoritative.\n2. Label every finding.\n"
                "3. Findings are about the subject.\n4. Name the gaps.\n"
                "5. Counts must match their lists.")
    assert rules._upstream_enumeration_size(upstream, "stage") == 4
    assert [v for v in rules.check({"summary": "It uses a nine-stage pipeline."},
                                   upstream=upstream, rules=rules.RESEARCH)
            if v.rule == "invented-numbering"]


def test_a_decimal_rate_is_not_read_as_an_enumerated_item():
    """AWS rates are full of "0.000005 USD"; none of it is a list.

    `_ENUM_ITEM_RE` requires whitespace after the dot for exactly this reason —
    cost_research upstream is mostly unit prices.
    """
    upstream = ("Pricing stages: S3 Requests-Tier1 at 0.000005 USD per Request; "
                "Glue USE1-Catalog-Storage at 0.00001 USD per Obj-Month.")
    assert rules._upstream_enumeration_size(upstream, "stage") == 0


def test_an_unrelated_numbered_list_far_from_the_noun_does_not_ground_it():
    """The list has to be the one the noun introduces, not merely present."""
    upstream = ("Stages matter for planning. " + "Filler prose. " * 30
                + "\n1. Collect\n2. Store\n3. Process\n4. Analyze")
    assert rules._upstream_enumeration_size(upstream, "stage") == 0


def test_an_unrelated_count_upstream_does_not_ground_a_plan():
    """The tolerance is narrow on purpose: `_STRUCTURE_NOUNS` names parts of a
    decomposition. "Seven services have published pricing" must not license a
    seven-phase rollout."""
    found = rules.check({"summary": "We propose a seven-phase rollout."},
                        upstream="Seven services have published pricing.",
                        rules=rules.SYNTHESIS)
    assert [v for v in found if v.rule == "invented-numbering"]


# ---------------------------------------------------------------------------
# action-on-unavailable: an unspecified requirement is not missing evidence
# ---------------------------------------------------------------------------

def test_asking_for_an_unspecified_requirement_is_a_real_action():
    """Live false positive, and the one that mattered most: the rule fired on the
    single most useful recommendation the run produced. "The source type is not
    provided" beside "Obtain and document: (1) data source type, (2) expected data
    volume…" is not a dead end — it is a person answering a question. Suppressing it
    to satisfy the check would have let the check damage the deliverable."""
    payload = {"items": [{
        "title": "Capture workload profile to unblock service selection",
        "detail": "Obtain and document: (1) data source type; (2) expected data "
                  "volume; (3) latency requirement; (4) budget or cost constraint.",
        "rationale": "Service selection is documented as purpose-built services "
                     "matched to data source type, but the source type is not "
                     "provided."}]}
    assert not [v for v in rules.check(payload, upstream="", rules=rules.SYNTHESIS)
                if v.rule == "action-on-unavailable"]


@pytest.mark.parametrize("payload", [
    {"items": [{"title": "Reuse prior work",
                "detail": "Consult the artifacts from the four prior runs.",
                "rationale": "Those artifacts are not available."}]},
    {"sections": [{"sectionType": "recommendations",
                   "content": "Attempt to retrieve prior run artifacts, which may "
                              "not be retrievable."}]},
    # Both kinds of noun present: the artifact wins, because a fetch aimed at one is
    # the defect and a false negative here is cheaper than re-breaking it.
    {"items": [{"title": "Get the volumes",
                "detail": "Retrieve the prior run output files.",
                "rationale": "The data volume is not provided and those outputs are "
                             "not available."}]},
])
def test_fetching_an_unavailable_artifact_still_fires(payload):
    """The defect the rule was built for, in both the blunt and hedged forms."""
    assert [v for v in rules.check(payload, upstream="", rules=rules.SYNTHESIS)
            if v.rule == "action-on-unavailable"]


# ---------------------------------------------------------------------------
# over-budget: the only threshold that is configuration
# ---------------------------------------------------------------------------

def test_no_budget_means_no_check():
    """Off by default. A framework that imposed a word count on a workflow it knows
    nothing about would be wrong more often than right."""
    fat = {"summary": "word " * 5000}
    assert not [v for v in rules.check(fat, upstream="", rules=rules.SYNTHESIS)
                if v.rule == "over-budget"]


def test_a_budget_is_enforced_and_names_the_longest_field():
    """The message has to be actionable: which fields to cut, and the two things not
    to do — drop a section, or compress by numbering (both observed live when the
    report was told to be shorter)."""
    payload = {"sections": [
        {"sectionType": "findings", "content": "word " * 100},
        {"sectionType": "recommendations", "content": "word " * 900},
    ]}
    found = [v for v in rules.check(payload, upstream="", rules=rules.SYNTHESIS,
                                   max_words=400) if v.rule == "over-budget"]
    assert len(found) == 1
    assert "1000 words against a budget of 400" in found[0].detail
    assert "recommendations" in found[0].detail, "name the field to cut"
    assert "numbering" in found[0].detail, "and the wrong way to get shorter"


def test_a_payload_inside_its_budget_is_silent():
    payload = {"sections": [{"sectionType": "findings", "content": "word " * 50}]}
    assert not [v for v in rules.check(payload, upstream="", rules=rules.SYNTHESIS,
                                       max_words=400) if v.rule == "over-budget"]


def test_the_budget_travels_from_config_to_the_check():
    """Config that does not reach the check is decoration. This is the whole path:
    workflow.json -> output_rules_for -> ask_json -> rules.check."""
    import inspect

    from app.common import structured
    from app.common.config import output_rules_for

    assert output_rules_for({"outputRules": {"maxWords": 250}})["maxWords"] == 250
    src = inspect.getsource(structured)
    assert 'settings.get("maxWords")' in src, "ask_json must read the config value"
    assert src.count("max_words=max_words") == 2, (
        "both the first check and the post-repair check need the budget, or a repair "
        "that stays over budget would look like an improvement")


def test_a_gap_and_a_fetch_a_thousand_words_apart_are_not_one_statement():
    """Live false positive. A report section is ONE entry and runs to a thousand
    words, so "private pricing and data transfer between services are not included"
    was paired with "Obtain from the requester: (1) data type…" from a different
    paragraph on a different subject."""
    content = ("Specify the workload profile to resolve service selection. Obtain "
               "from the requester: (1) data type; (2) expected volume. "
               + "Filler prose about the architecture. " * 90
               + "Free-tier allowances, committed-use discounts, private pricing and "
                 "data transfer between services are not included.")
    payload = {"sections": [{"sectionType": "recommendations", "content": content}]}
    assert not [v for v in rules.check(payload, upstream="", rules=rules.SYNTHESIS)
                if v.rule == "action-on-unavailable"]


def test_the_reach_across_one_entry_is_preserved():
    """The rule deliberately spans an item's `detail` and its `rationale`, two
    sentences apart — that is the case it was built for, and narrowing the window
    must not lose it."""
    payload = {"items": [{"title": "Reuse prior work",
                          "detail": "Consult the artifacts from the four prior runs.",
                          "rationale": "Those artifacts are not available."}]}
    assert [v for v in rules.check(payload, upstream="", rules=rules.SYNTHESIS)
            if v.rule == "action-on-unavailable"]


def test_a_tie_that_is_genuinely_shorter_is_kept(structured):
    """`over-budget` is a MAGNITUDE, and counting violations cannot see it. Measured
    on a real report: a re-ask cut it from 2453 words to 1704 against an 1800 budget
    and was thrown away, because one `over-budget` violation before still meant one
    after. The extra call is already spent at that point."""
    fat = '{"summary": "' + ("word " * 300).strip() + '"}'
    lean = '{"summary": "' + ("word " * 120).strip() + '"}'
    ctx = _FakeCtx(fat, lean, max_words=100)
    payload, unrepaired = _run(structured.ask_json(
        ctx, "sys", "user", rule_set=rules.SYNTHESIS, upstream=""))
    assert len(ctx.calls) == 2
    assert len(payload["summary"].split()) == 120, "the shorter answer must win"
    assert len(unrepaired) == 1, "and it is still over budget, which is recorded"


def test_a_tie_that_is_not_shorter_keeps_the_first_answer(structured):
    """The other half, and the reason a tie is not accepted blindly: a rewrite with
    the same count can have dropped real content. Here it loses `limitations` and
    adds another invented label."""
    ctx = _FakeCtx('{"summary": "Phase 1 then Phase 2.", "limitations": ["real gap"]}',
                   '{"summary": "Phase 1 then Phase 2 then Phase 3."}')
    payload, unrepaired = _run(structured.ask_json(
        ctx, "sys", "user", rule_set=rules.SYNTHESIS, upstream=""))
    assert payload.get("limitations") == ["real gap"]
    assert len(unrepaired) == 1


# ---------------------------------------------------------------------------
# A check that was removed, and the evidence for removing it
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    # The two real defects it used to catch. Both now go unflagged, which is the
    # accepted cost — recorded here so the trade is visible rather than forgotten.
    ("Workflows require nine core best practices: tool-first design, pure-function "
     "invocation, single-tool agents, externalized prompts, model-consortium design, "
     "clean separation, containerized deployment, and the KISS principle."),
    ("Pipelines decompose into five functional layers: ingestion, processing, "
     "storage/cataloging, and consumption."),
    # And the eighth false-positive shape, the one that ended it: five layers are
    # named and the sixth "item" is a trailing participial clause.
    ("Serverless architectures decompose into five logical layers: event "
     "trigger/interface, orchestration, service, data, consumption, enabling "
     "independent scaling and recovery."),
])
def test_a_count_against_prose_is_no_longer_checked(text):
    """The inline-enumeration check was REMOVED. It compared a stated count against
    the list in the same sentence, which `_check_counts` cannot do because prose is
    not a schema field.

    It caught two real defects. It also produced eight distinct false-positive shapes
    on real output inside one day — a conjunction inside a clause, a narrowing word in
    the noun capture, "Lambda vs. Glue" truncating the sentence, a verb between the
    count and the colon, a nested sub-list after a dash, long clauses with a subset
    quoted, compound items over-split, and a trailing participial clause counted as an
    item. On its final run both firings were false and neither was a defect.

    Deciding what a colon governs is parsing English, and every narrowing moved the
    rule closer to matching only its own fixtures. A check a reviewer learns to
    distrust is worse than no check, because they stop reading the panel.

    If this needs covering again: do it in the prompts, or change the schema so the
    enumeration is a field. Do not re-add a ninth narrowing."""
    assert not [v for v in rules.check({"summary": text}, upstream="",
                                       rules=rules.SYNTHESIS)
                if v.rule == "count-mismatch"], text[:60]


def test_the_reliable_half_of_the_count_check_survives():
    """`_check_counts` compares a stated count against a list the PAYLOAD ACTUALLY
    HAS, where there is nothing to parse and nothing to get wrong. That is the half
    worth keeping, and removing its sibling must not have taken it with it."""
    payload = {"summary": "Nine open questions on the subject remain unresolved."}
    found = rules.check(payload, upstream="", rules=rules.SYNTHESIS,
                        extra_counts={"openQuestions": 5, "keyQuestions": 6})
    assert [v for v in found if v.rule == "count-mismatch"]
    assert "5 or 6" in found[0].detail
