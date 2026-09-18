"""The output rules, run over COMPLETE real assets, with every firing adjudicated.

WHY THIS FILE EXISTS
--------------------
tests/test_output_rules.py tests each rule against text I had already judged to be
wrong. That suite passed 47/47 on the first attempt and the rules still shipped a
60% false-positive rate on their first live run. The reason is structural: a suite
whose positive cases are defects I spotted, and whose negative cases are false
positives I could imagine, can only ever confirm the model that wrote it. The
false positive that actually shipped — flagging the brief's own declared
`assumptions` when a downstream agent carried them forward — was not something I
had thought to imagine, so nothing tested it.

This file fixes that. It runs the rules over every field of every asset from a real
run and asserts the EXACT set of violations, including the many fields that are
correct. A rule that starts firing on good content fails here.

The adjudication (run of 2026-09-16 18:22, ten firings):
  1. history_research findings[2]  "The request brief identifies six open …"   TRUE
  2. knowledge_research findings[4] "The request brief assumes the user …"      TRUE
  3. documentation_search "four stage" structure                              FALSE
     -> upstream defines Stage 1..Stage 4; counting them is not inventing them
  4. documentation_search findings[5] "The request brief identifies six key …"  TRUE
  5. web_search figure "2025–2026"                                            FALSE
     -> a publication-year range; no reader mistakes it for a commitment
  6. analysis claims[3] "Seven prior runs … completed successfully"            TRUE
  7. analysis assumptions[0] "The user wants a working application …"          FALSE
  8. analysis assumptions[1] "The user is familiar with basic AI/ML …"         FALSE
  9. analysis assumptions[2] "The user has access to necessary APIs …"         FALSE
 10. analysis assumptions[3] "The request brief's open questions represent …"   FALSE
  + MISSED: knowledge_research findings[5] "The request identifies six key …"

Four true, six false, one missed. The six false positives traced to three causes,
each fixed structurally rather than by exception:
  * no distinction between what an asset ASSERTS and what it DISCLOSES
    (rules._DISCLOSURE_KEYS)
  * grounding tested by literal phrase instead of by the series
    (rules._upstream_series_size)
  * identifiers — years, versions, model names — counted as quantities
    (rules._FIGURE_RE now requires a quantity marker)

The NEXT run (18:46, session 528f3123e455) fired eighteen times: fifteen true and
three more false. Those three are pinned at the bottom of this file:
  * an en-dash range could NEVER be grounded, because `assets.for_prompt` escaped
    non-ASCII and the upstream text held "\\u2013" instead of the character
    (assets.for_prompt ensure_ascii=False, rules._canon_figure dash unification)
  * "four research sources" checked against a five-entry `sources` list that also
    holds the brief — provenance is not a countable claim (rules._COUNT_TARGETS)
  * "the five prior recommendations" checked against this payload's six items — a
    count that narrows to a subset is not a total (rules._NARROWING)
That run also confirmed the fix for the leak (no agent was shown a sibling's
ruleViolations) and that `invented-numbering` did not fire once in eight agents,
after five releases of prompt edits failed to stop it.
"""

import json
from pathlib import Path

import pytest

from app.common import assets, rules, synthesis

CORPUS = json.loads(
    (Path(__file__).parent / "fixtures" / "real_run_18_22.json").read_text())

PROFILES = {"BRIEF": rules.BRIEF, "RESEARCH": rules.RESEARCH,
            "SYNTHESIS": rules.SYNTHESIS}


# ---------------------------------------------------------------------------
# `upstream` is BUILT, not typed
# ---------------------------------------------------------------------------
#
# The first version of this file carried a hand-abridged prose `upstream` per
# agent, and that paraphrase hid a real defect for a whole release:
# `assets.for_prompt` serialised upstream assets with `json.dumps` defaults, so
# every non-ASCII character was escaped and an en dash reached the model as the
# six characters \u2013. No figure written "2–4 weeks" could be grounded against
# anything. My fixture contained a literal en dash, so the test agreed with itself
# while production disagreed with the model.
#
# So the corpus stores the INPUTS (the tool evidence, the topology) and the text
# is assembled here by the same functions the runners call. If `for_prompt` or
# `upstream_context` changes shape, these tests see it.


class _CtxStub:
    """Just enough ctx for `synthesis.upstream_context`: an id -> stored asset map.

    In a live run `ctx.input(agent_id)` returns the upstream asset as the JSON
    string it was stored as, which is what the corpus payloads are dumped to here.
    """

    def __init__(self, payloads: dict[str, dict]):
        self._payloads = payloads

    def input(self, agent_id: str) -> str | None:
        payload = self._payloads.get(agent_id)
        return json.dumps(payload) if payload else None


def _payloads() -> dict[str, dict]:
    return {k: v["payload"] for k, v in CORPUS.items() if not k.startswith("_")}


def _upstream_for(agent: str) -> str:
    """The text the agent was shown, assembled the way its runner assembles it."""
    spec = CORPUS[agent]
    if "upstream" in spec:          # a foreign run; its inputs are not in the corpus
        return spec["upstream"]
    if "topic" in spec:             # the first agent: no upstream asset exists yet
        return spec["topic"]
    if "evidence" in spec:          # research.synthesize: brief + tool evidence
        brief = assets.for_prompt(CORPUS["intake"]["payload"])
        return f"{brief}\n{spec['evidence']}"
    # synthesis.synthesize: the approved upstream assets, rendered for the prompt
    context, _ids = synthesis.upstream_context(
        _CtxStub(_payloads()), spec["upstreamFrom"])
    return context

# The adjudicated truth: agent -> the violations its real output SHOULD produce.
# Every entry here is a defect a reviewer would want flagged. Every asset absent
# from this mapping must produce nothing at all.
#
# The three `summary` entries were added when finding-about-the-brief grew past
# findings/claims to cover the reader-facing prose fields. All three assets open
# their summary with "The request brief seeks design and build guidance for an
# agentic AI application but lacks domain and use-case specificity" — the same
# defect the rule already caught inside findings, sitting in the field with the
# widest audience. They are new catches on old output, not new output.
EXPECTED: dict[str, list[tuple[str, str]]] = {
    "history_research": [("finding-about-the-brief", "findings[2]")],
    "knowledge_research": [("finding-about-the-brief", "findings[4]"),
                           ("finding-about-the-brief", "findings[5]"),
                           ("finding-about-the-brief", "summary")],
    "documentation_search": [("finding-about-the-brief", "findings[5]"),
                             ("finding-about-the-brief", "summary")],
    "analysis": [("about-the-request", "claims[3]")],
    "web_search": [("finding-about-the-brief", "summary")],
    # Correct output, and the reason this file exists.
    "intake": [],
    "recommendation": [],
    "report": [],
}

AGENTS = sorted(k for k in CORPUS if not k.startswith("_"))

# The report from the run BEFORE the rules existed. A corpus of clean assets can
# be satisfied by a rule that never fires, so the test has to run both ways.
KNOWN_BAD = "_known_bad_report_17_51"
KNOWN_BAD_EXPECTED = [
    ("about-the-request", "sections[1]"),
    ("action-on-unavailable", "sections[1](recommendations)"),
    ("invented-numbering", "ordinal markers First, Second, Third"),
    ("unsupported-figure", '"$50,000"'),
    ("unsupported-figure", '"2 weeks"'),
]


def _check(agent: str) -> list[rules.Violation]:
    spec = CORPUS[agent]
    return rules.check(spec["payload"], upstream=_upstream_for(agent),
                       rules=PROFILES[spec["profile"]],
                       extra_counts=spec.get("brief_counts") or {})


def test_a_real_pre_rules_report_still_produces_every_one_of_its_defects():
    """The other direction. This is the 17:51 report, written before any of this
    was enforced: an invented ordinal running order, two figures no input
    supplied, run history as report content, and an action item that tells the
    reader to fetch something it admits is unavailable. All five must fire.
    """
    found = _check(KNOWN_BAD)
    actual = sorted((v.rule, v.detail) for v in found)
    assert len(actual) == len(KNOWN_BAD_EXPECTED), (
        "\n".join(f"  [{r}] {d[:150]}" for r, d in actual))
    for (rule, fragment), (got_rule, got_detail) in zip(KNOWN_BAD_EXPECTED, actual):
        assert got_rule == rule
        assert fragment in got_detail, f"{rule}: {got_detail[:150]}"


def test_the_corpus_covers_every_agent_in_the_pipeline():
    """A corpus missing an agent silently stops testing it."""
    assert len(AGENTS) == 8
    assert set(AGENTS) == set(EXPECTED)


@pytest.mark.parametrize("agent", AGENTS)
def test_real_asset_produces_exactly_the_adjudicated_violations(agent):
    found = _check(agent)
    actual = sorted((v.rule, v.detail) for v in found)
    expected = EXPECTED[agent]

    assert len(found) == len(expected), (
        f"{agent}: expected {len(expected)} violation(s), got {len(found)}:\n"
        + "\n".join(f"  [{r}] {d[:160]}" for r, d in actual))
    for (rule, where), (got_rule, got_detail) in zip(expected, actual):
        assert got_rule == rule, f"{agent}: expected {rule}, got {got_rule}"
        assert where in got_detail, (
            f"{agent}: expected the {rule} to be about {where}, got: {got_detail[:160]}")


# ---------------------------------------------------------------------------
# The three root causes, each pinned to the real text that exposed it
# ---------------------------------------------------------------------------

def test_the_briefs_own_assumptions_survive_being_carried_forward():
    """Root cause A. Four of the six false positives were this.

    An `assumptions` list exists to declare the premises the work rests on. The
    about-* rules exist to stop an INVENTED claim being used as a finding or a
    rationale. Conflating the two meant the rule attacked the honest behaviour.
    """
    payload = {"summary": "Agentic systems need durable state.",
               "assumptions": [("The user wants a working application rather than "
                                "theoretical design."),
                               "The user is familiar with basic AI/ML concepts."],
               "limitations": [("The content of the seven prior completed runs is "
                                "not available to this analysis.")]}
    assert rules.check(payload, upstream="durable state",
                       rules=rules.SYNTHESIS) == []


def test_the_same_claim_in_a_rationale_is_still_caught():
    """The other half of root cause A: narrowing the scope must not disarm it.

    This is the sentence the rule was written for — an invented fact about the
    requester, load-bearing for a recommendation.
    """
    payload = {"items": [{"title": "Use Lambda",
                          "rationale": "The user's background is hands-on with "
                                       "AWS serverless, so Lambda is feasible."}]}
    found = rules.check(payload, upstream="", rules=rules.SYNTHESIS)
    assert [v.rule for v in found] == ["about-the-request"]


def test_counting_a_series_the_inputs_define_is_not_inventing_one():
    """Root cause B. AWS documentation defines Stage 1 through Stage 4 without
    ever writing the words "four stages"; reporting "four stages" is counting the
    evidence. Requiring the literal phrase upstream made that a violation."""
    upstream = ("Stage 1: More human oversight. Stage 2: Generative AI agents. "
                "Stage 3: Agentic AI systems. Stage 4: Autonomous AI agents.")
    payload = {"summary": "Agentic AI evolves through four stages, from Stage 1 "
                          "to Stage 4."}
    assert rules.check(payload, upstream=upstream, rules=rules.SYNTHESIS) == []


def test_a_compound_larger_than_the_series_is_still_invented():
    """The other half of root cause B: two grounded stages do not license five."""
    found = rules.check({"summary": "We propose a five-stage rollout."},
                        upstream="Stage 1 and Stage 2 are documented.",
                        rules=rules.SYNTHESIS)
    assert [v.rule for v in found] == ["invented-numbering"]


@pytest.mark.parametrize("identifier", [
    "sources published across 2025–2026",        # the actual false positive
    "LangGraph reached v0.4 in early 2026",
    "models such as GPT-4 and Llama 3.3",
    "Mixtral 8x22 supports tool calling",
])
def test_identifiers_are_not_quantities(identifier):
    """Root cause C. A year, a version and a model name cannot be mistaken for a
    budget or a deadline, which is the only harm the figure rule addresses."""
    found = rules.check({"summary": identifier}, upstream="", rules=rules.SYNTHESIS)
    assert [v.rule for v in found] == []


@pytest.mark.parametrize("commitment", [
    "approve expenses under $5,000",
    "confidence below 80%",
    "delivery in 6-12 months",
    "onboard 1,200 users",
    "a review every 2 weeks",
    "staffed by 3 engineers",
])
def test_anything_a_reader_could_read_as_a_commitment_is_still_caught(commitment):
    """The other half of root cause C: requiring a quantity marker must not
    disarm the rule. Every shape that carries an obligation still carries a
    marker — a currency symbol, a percent sign, or a unit."""
    found = rules.check({"summary": commitment}, upstream="", rules=rules.SYNTHESIS)
    assert [v.rule for v in found] == ["unsupported-figure"], commitment


def test_a_bare_number_with_no_unit_is_deliberately_not_flagged():
    """Named honestly rather than hidden: requiring a marker trades recall for
    precision. "under 5000" with no currency or unit is missed. That is the
    accepted cost of a check that does not cry wolf — a false positive spends a
    repair call and degrades the deliverable, which is worse than this gap."""
    assert rules.check({"summary": "approve expenses under 5000"},
                       upstream="", rules=rules.SYNTHESIS) == []


def test_the_brief_is_recognised_however_it_is_named():
    """Root cause D, the missed defect: matching only "request brief" let the same
    sentence through under a shorter name."""
    for phrasing in ("The request brief identifies six open questions.",
                     "The request identifies six key open questions.",
                     "This request specifies the target domain.",
                     "The brief's scope excludes production code."):
        payload = {"findings": [{"statement": phrasing,
                                 "classification": "agent-interpretation"}]}
        found = rules.check(payload, upstream="", rules=rules.RESEARCH)
        assert [v.rule for v in found] == ["finding-about-the-brief"], phrasing


def test_a_finding_about_the_subject_that_mentions_a_request_survives():
    """The rule must not fire on the ordinary noun."""
    payload = {"findings": [{"statement": "A router pattern classifies each "
                                          "request and picks an agent.",
                             "classification": "sourced-fact"}]}
    assert rules.check(payload, upstream="router pattern classifies",
                       rules=rules.RESEARCH) == []


# ---------------------------------------------------------------------------
# Upstream assets must not carry rule commentary into the next agent's evidence
# ---------------------------------------------------------------------------

def test_rule_violations_are_stripped_from_a_downstream_prompt():
    """Root cause E. In the first live run every synthesis agent was shown the
    upstream assets' `ruleViolations` as part of its evidence — one agent's
    critique of another, presented as research."""
    from app.common import assets

    rendered = assets.for_prompt({
        "assetId": "asset-research-history_research-x-v2",
        "summary": "Seven prior runs completed.",
        "ruleViolations": [("finding-about-the-brief: findings[2] restates the "
                            "request brief rather than reporting evidence")]})
    assert "ruleViolations" not in rendered
    assert "restates the request brief" not in rendered
    assert "Seven prior runs completed." in rendered
    assert "asset-research-history_research-x-v2" in rendered


def test_for_prompt_falls_back_cleanly_on_an_empty_asset():
    from app.common import assets

    assert assets.for_prompt({}) == ""

# ---------------------------------------------------------------------------
# The 18:46 run: three more false positives, each pinned to the real text
# ---------------------------------------------------------------------------

def test_an_en_dash_range_is_grounded_through_the_real_render_path():
    """Root cause F, and the reason this file no longer hand-types `upstream`.

    `assets.for_prompt` used json.dumps defaults, so `ensure_ascii=True` turned
    every en dash in an upstream asset into the six characters \\u2013. The
    character was therefore NEVER in the text the model was shown, and any figure
    quoting a range with an en dash was reported ungrounded no matter how faithful
    it was. The rule looked correct in isolation and was unfalsifiable in practice.

    Asserted through `for_prompt` on purpose: a hand-written upstream string with a
    literal en dash passes this test even with the bug present, which is exactly
    how it survived a release.
    """
    upstream = assets.for_prompt({
        "assetId": "asset-research-web_search-x-v1",
        "summary": "Vendors quote delivery in 2\u20134 weeks for a pilot."})

    assert "2\u20134" in upstream, "for_prompt escaped the en dash again"
    assert rules.check({"summary": "A pilot lands in 2\u20134 weeks."},
                       upstream=upstream, rules=rules.SYNTHESIS) == []


@pytest.mark.parametrize("written", ["2-4 weeks", "2\u20134 weeks", "2\u20144 weeks"])
def test_a_range_is_the_same_quantity_whichever_dash_it_uses(written):
    """The other half of root cause F. `_FIGURE_RE` accepts three dashes, so the
    comparison has to unify them or grounding depends on typography."""
    upstream = assets.for_prompt({"summary": "delivery in 2\u20134 weeks"})
    assert rules.check({"summary": f"expect {written}"},
                       upstream=upstream, rules=rules.SYNTHESIS) == []


def test_an_ungrounded_range_is_still_caught_whichever_dash_it_uses():
    """Unifying dashes must not ground a figure nobody supplied."""
    upstream = assets.for_prompt({"summary": "delivery in 2\u20134 weeks"})
    found = rules.check({"summary": "expect 6\u201312 months"},
                        upstream=upstream, rules=rules.SYNTHESIS)
    assert [v.rule for v in found] == ["unsupported-figure"]


def test_a_count_of_sources_is_not_checked_against_the_sources_list():
    """Root cause G. `sources` is provenance, and the list carries one entry per
    artifact the agent was handed — including the request brief and any recalled
    context. So an agent that correctly reports the four research tools it
    consulted was flagged against a five-entry list. The mismatch was in the
    question, not the output."""
    payload = {"summary": "Synthesised from four research sources across the run.",
               "sources": [{"sourceId": "request-brief"},
                           {"sourceId": "kb"}, {"sourceId": "web"},
                           {"sourceId": "docs"}, {"sourceId": "runs"}]}
    assert rules.check(payload, upstream="", rules=rules.SYNTHESIS) == []


def test_a_count_that_narrows_to_a_subset_is_not_read_as_a_total():
    """Root cause H. "the five prior recommendations" describes what an earlier
    version carried; it is not a claim about the six items in this payload."""
    payload = {"summary": "This version carries the five prior recommendations "
                          "forward and adds one.",
               "items": [{"title": f"recommendation {i}"} for i in range(6)]}
    assert rules.check(payload, upstream="", rules=rules.SYNTHESIS) == []


@pytest.mark.parametrize("phrase", [
    "three remaining open questions",
    "two other risks",
    "the first four findings",
    "four additional constraints",
])
def test_every_narrowing_modifier_is_recognised(phrase):
    payload = {"summary": f"There are {phrase}.",
               "openQuestions": ["a"] * 6, "risks": ["b"] * 6,
               "findings": [{"statement": "s"}] * 6, "constraints": ["c"] * 6}
    assert [v.rule for v in rules.check(payload, upstream="",
                                        rules=rules.SYNTHESIS)] == []


def test_a_narrowing_word_in_the_previous_clause_does_not_exempt_the_count():
    """The modifier has to be attached to the count. Looking back two words is
    enough to catch "the first four findings" without letting a comma-separated
    clause disarm a real total."""
    payload = {"summary": "In the next section, five recommendations follow.",
               "items": [{"title": str(i)} for i in range(6)]}
    found = rules.check(payload, upstream="", rules=rules.SYNTHESIS)
    assert [v.rule for v in found] == ["count-mismatch"]


@pytest.mark.parametrize("phrase,key,items", [
    ("five recommendations", "items", 6),
    ("seven open questions", "openQuestions", 6),
    ("three critical risks", "risks", 4),
])
def test_a_plain_total_that_contradicts_its_list_is_still_caught(phrase, key, items):
    """The other half of root cause H: exempting subsets must not exempt totals.
    This is the case the rule was built for — a stated count a reader can check
    against the list printed directly beneath it."""
    payload = {"summary": f"This report makes {phrase}.",
               key: [{"title": str(i)} for i in range(items)]}
    found = rules.check(payload, upstream="", rules=rules.SYNTHESIS)
    assert [v.rule for v in found] == ["count-mismatch"], phrase


# ---------------------------------------------------------------------------
# The corpus itself
# ---------------------------------------------------------------------------

def test_the_corpus_stores_inputs_rather_than_a_paraphrase_of_them():
    """The meta-fix. A hand-typed `upstream` tests the paraphrase, not the
    pipeline — that is how the escaped-en-dash defect survived a release. Every
    agent's upstream text must be derivable from the inputs the corpus records,
    so a change to `for_prompt` or `upstream_context` shows up here.
    """
    for agent in AGENTS:
        spec = CORPUS[agent]
        assert "upstream" not in spec, (
            f"{agent}: `upstream` is hand-typed prose. Record the INPUTS "
            f"(`evidence` or `upstreamFrom`) and let the runner's own code "
            f"assemble the text.")
        assert ("evidence" in spec) or ("upstreamFrom" in spec) or ("topic" in spec)


def test_a_synthesis_agents_upstream_is_the_rendered_upstream_assets():
    """The derived text must be what the runner produces: labelled blocks of the
    upstream assets as JSON, not a summary of them."""
    upstream = _upstream_for("report")
    assert upstream.startswith("=== RECOMMENDATION ===")
    for label in ("ANALYSIS", "HISTORY RESEARCH", "WEB SEARCH", "INTAKE"):
        assert f"=== {label} ===" in upstream
    # Whole assets, not an abridgement: the hand-typed version was 326 characters.
    assert len(upstream) > 10_000
    assert "ruleViolations" not in upstream


# ---------------------------------------------------------------------------
# The 19:24 run: two more false positives
# ---------------------------------------------------------------------------

def test_a_count_matching_a_sibling_list_is_not_a_mismatch():
    """Root cause I. English does not draw the distinction the schema does.

    The brief held six `keyQuestions` and five `openQuestions`. An agent wrote
    "the six open questions identified in the request brief" and then enumerated
    the six keyQuestions verbatim — the count was right about a real six-item
    list, and the rule called it a mismatch because the noun it happened to use
    resolved to the other list. The check cannot know which list the prose meant,
    so a number matching either one is not a contradiction.
    """
    payload = {"summary": "Six open questions on this subject (use case, "
                          "autonomous scope, constraints, tool access, tech stack, "
                          "success metrics) are prerequisites.",
               "items": [{"title": str(i)} for i in range(9)]}
    brief = {"openQuestions": 5, "keyQuestions": 6, "constraints": 0,
             "assumptions": 3}
    assert rules.check(payload, upstream="", rules=rules.SYNTHESIS,
                       extra_counts=brief) == []


def test_a_count_matching_no_list_at_all_is_still_a_mismatch():
    """The other half of root cause I: accepting either sibling must not accept a
    number that matches neither, which is the case the rule was built for."""
    payload = {"summary": "Nine open questions on the subject remain unresolved."}
    brief = {"openQuestions": 5, "keyQuestions": 6}
    found = rules.check(payload, upstream="", rules=rules.SYNTHESIS,
                        extra_counts=brief)
    assert [v.rule for v in found] == ["count-mismatch"]
    assert "5 or 6" in found[0].detail, "name both candidates so the fix is obvious"


def test_naming_what_a_missing_input_would_unblock_is_not_an_action():
    """Root cause J, and the rule punishing its own remedy.

    `action-on-unavailable` tells the model to "state what is missing and what
    having it would unblock". The report did exactly that — and was flagged again,
    because a conditional ("obtaining this would enable X") contains a fetch verb
    and the section also named the gap. A description of what an absent thing would
    make possible is disclosure, not a step the reader is being handed.
    """
    payload = {"sections": [{
        "sectionType": "next-steps",
        "content": "Six critical information gaps prevent proceeding to "
                   "architecture design. The intended use case and domain are not "
                   "specified; obtaining this would enable grounding the "
                   "recommendations. Success metrics are not specified; obtaining "
                   "this would enable objectively assessing progress."}]}
    assert rules.check(payload, upstream="", rules=rules.SYNTHESIS) == []


def test_a_gap_and_a_conditional_in_separate_sentences_are_not_a_contradiction():
    """The same false positive in its original, longer shape: the gap named in one
    sentence and the conditional three sentences later."""
    payload = {"sections": [{
        "sectionType": "next-steps",
        "content": "The six gaps are not independent. Their artifacts and outputs "
                   "are not available. If prior runs produced reusable code, "
                   "retrieving those assets would accelerate the current effort."}]}
    assert [v.rule for v in rules.check(payload, upstream="",
                                        rules=rules.SYNTHESIS)
            if v.rule == "action-on-unavailable"] == []


def test_an_action_and_its_own_impossibility_still_pair_across_an_entry():
    """The other half of root cause J, and a regression I introduced while fixing
    it. Scoping the pairing to a single sentence looked tidy and lost the ORIGINAL
    defect: the step lives in `detail` and the gap in `rationale`, two fields of
    one item, which a reader takes as one statement."""
    payload = {"items": [{"title": "Reuse prior work",
                          "detail": "Consult the artifacts from the four prior runs.",
                          "rationale": "Those artifacts are not available."}]}
    found = rules.check(payload, upstream="", rules=rules.SYNTHESIS)
    assert [v.rule for v in found if v.rule == "action-on-unavailable"], (
        "the pairing must reach across an entry's fields, not just one sentence")
