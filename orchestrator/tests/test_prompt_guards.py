"""Instructions that exist because a live run produced the defect they forbid.

A prompt is the easiest thing in this repo to "tidy" — the wording reads like
commentary, and deleting it costs nothing until the next run. Every assertion here
names the run that made the instruction necessary, so a future edit that removes one
fails with the reason attached instead of silently reopening a fixed defect.

These are cheap string checks on purpose. Whether the model OBEYS is not something a
unit test can settle; that is verified by probing the real model and then by reading
`ruleViolations` off the next live run. What this file pins is that the instruction
is still there to obey.
"""
import json

from conftest import ORCH_ROOT

from app.subagents.analysis import prompts as analysis
from app.subagents.intake import prompts as intake
from app.subagents.recommendation import prompts as recommendation
from app.subagents.report import prompts as report

WORKFLOW = json.loads((ORCH_ROOT / "app" / "workflow.json").read_text())


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------

def test_the_report_is_told_not_to_transcribe_upstream():
    """A recommendations section that reproduced fourteen upstream items
    near-verbatim, in a report that cost more to generate than the seven agents
    before it combined."""
    p = report.SYSTEM_PROMPT.lower()
    assert "assemble, do not transcribe" in p
    assert "say it once" in p


def test_the_report_is_told_that_compressing_is_not_numbering():
    """Asked to condense, the model reached for "First, … Second, … Seventh, …" —
    turning a list it was told not to copy into a running order it was told not to
    invent. Both instructions have to sit together or the second one loses."""
    p = report.SYSTEM_PROMPT.lower()
    assert "compressing is not numbering" in p
    assert "do not schedule, phase or number the work" in p


def test_the_report_is_told_a_sources_figure_is_not_about_this_work():
    """Twice live. "2–4 weeks" from an article about other people's projects, beside
    a sentence saying no timeline was supplied; and a background section reporting a
    reference pipeline "incurred less than $1 USD in test deployment costs" — true
    of somebody's tutorial, read as the cost of the proposed architecture.

    The clause covers cost AND duration AND throughput deliberately: the first
    version named durations only, and the very next run carried a dollar figure."""
    p = report.SYSTEM_PROMPT.lower()
    assert "a figure from a source is not a figure about this work" in p
    for kind in ("cost", "duration", "throughput"):
        assert kind in p, kind
    assert "$1 usd" in p, "keep the observed example; it is what makes the rule land"


# ---------------------------------------------------------------------------
# recommendation
# ---------------------------------------------------------------------------

def test_the_recommendation_agent_is_told_to_merge_one_decision():
    """Four of thirteen items were the same fact wearing four titles: the workload
    profile is unknown. `duplicate-in-list` cannot catch it — the four share almost
    no wording, and that check is deliberately lexical."""
    p = recommendation.SYSTEM_PROMPT.lower()
    assert "one decision, one item" in p
    assert "prefer fewer, larger items" in p


# ---------------------------------------------------------------------------
# intake
# ---------------------------------------------------------------------------

def test_intake_is_told_to_commit_to_an_interpretation():
    """The cascade: a brief that put the substance of the request out of scope left
    seven agents with nothing to work on but the gaps, and the run's deliverable was
    a list of questions."""
    p = intake.SYSTEM_PROMPT.lower()
    assert "commit to a working interpretation" in p
    assert "empty list" in p, "an empty outOfScope must stay the encouraged answer"


def test_intake_is_told_the_two_question_lists_are_disjoint():
    """Six keyQuestions and six openQuestions, three pairs of which were one
    question in two wordings, carried by all eight agents downstream."""
    p = intake.SYSTEM_PROMPT.lower()
    assert "disjoint" in p
    assert "could evidence settle" in p, "the tie-break test, not just the ban"


# ---------------------------------------------------------------------------
# analysis
# ---------------------------------------------------------------------------

def test_the_analysis_rationale_is_defined_as_weighing_evidence():
    """`rationale` used to be described as "why this analysis, grounded in the
    evidence", which the model answered by narrating the request."""
    p = analysis.SYSTEM_PROMPT.lower()
    assert "how you weighed the evidence" in p
    assert "not a description of the request" in p


def test_repair_is_on_only_where_the_defect_is_mechanical():
    """Repair costs a model call, so where it is spent is a measured decision.

    It was tried on `analysis` first and FAILED: that agent's defect is semantic —
    write about the subject, not the request — and the re-ask rewrote the field into
    the same defect class. One violation before, one after, $0.022 for nothing.

    It is on for `report` and `recommendation`, whose defects are MECHANICAL: too
    long, ordinal markers it was told not to use, a threshold no upstream asset
    contains. "Cut this to the budget", "remove First, Second, Third" and "remove the
    figure 90 days" are instructions a model can execute against a quoted violation,
    where "write about a different subject" is not. That distinction is the evidence,
    and it is why this is per-agent config rather than an engine default. It is also
    borne out: the run after repair went on for `report` returned that agent clean,
    at 1094 words against an 1800 budget, down from 2600.

    The engine default stays checks-only: a framework must not spend a customer's
    money on a second call by default."""
    assert WORKFLOW["orchestrator"]["outputRules"] == {"enabled": True,
                                                      "repair": False}, \
        "checks stay ON; the repair CALL stays off by default"
    on = {name for name, spec in WORKFLOW["agents"].items()
          if (spec.get("outputRules") or {}).get("repair")}
    assert on == {"report", "recommendation"}, (
        f"repair enabled for {on or 'nothing'}. It was measured as ineffective on "
        f"`analysis` — see that agent's prompts.py before adding another.")


def test_a_length_budget_is_config_and_defaults_to_off():
    """`maxWords` is the only rule threshold that is config, because length is the
    only defect here with no correct value a framework could know. It exists because
    prompting failed three times on one agent (2164 -> 1749 -> 2600 words), and it
    must stay OFF by default so the framework imposes no house style on a workflow it
    knows nothing about."""
    from app.common.config import OUTPUT_RULES, output_rules_for

    assert OUTPUT_RULES["maxWords"] == 0, "no budget unless a workflow asks for one"
    assert output_rules_for({})["maxWords"] == 0
    assert output_rules_for({"outputRules": {"maxWords": 900}})["maxWords"] == 900
    budgeted = {name for name, spec in WORKFLOW["agents"].items()
                if (spec.get("outputRules") or {}).get("maxWords")}
    assert budgeted == {"report"}, f"budget set on {budgeted or 'nothing'}"


def test_a_budget_is_set_only_where_length_was_the_defect():
    """`maxWords` and `repair` are separate dials and are deliberately not paired.
    `recommendation` gets repair without a budget: it invented a threshold, which is
    mechanical, but its length has never been a problem — and a budget that never
    fires is a rule a reviewer learns to ignore."""
    budgeted = {name for name, spec in WORKFLOW["agents"].items()
                if (spec.get("outputRules") or {}).get("maxWords")}
    assert budgeted == {"report"}, f"budget set on {budgeted or 'nothing'}"


def test_the_research_agents_are_told_the_summary_is_about_the_subject():
    """The last place `finding-about-the-brief` survived: a web_search summary reading
    "The retrieved sources directly address the request's scope and key questions".
    Rule 3 covered findings and never mentioned `summary`, which is the line most
    likely to be read."""
    from app.common.research import RESEARCH_INSTRUCTIONS as block

    assert "RULE 3 APPLIES TO `summary` TOO" in block
    assert "retrieved sources directly address" in block, "keep the observed example"
    # All four research agents share this block, so the fix reaches every one of them
    # rather than the single agent that happened to produce the defect.
    assert "A FINDING IS ABOUT THE SUBJECT" in block
