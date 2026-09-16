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


def test_run_history_is_allowed_where_it_is_the_subject():
    """The history_research agent exists to report prior runs, so the rule is off
    in its profile. A rule that fired here would break the agent it applies to."""
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
    """Enough of AgentContext for ask_json: llm(), log(), agent_id."""

    def __init__(self, *answers: str):
        self.answers = list(answers)
        self.agent_id = "analysis"
        self.calls: list[str] = []
        self.logs: list[str] = []

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
