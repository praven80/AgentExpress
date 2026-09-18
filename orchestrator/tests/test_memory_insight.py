"""What reaches long-term memory — the one write whose mistakes outlive the run.

A bad prompt produces a bad report and the next run gets another chance. A bad
long-term memory record is recalled by EVERY later run until someone deletes the
namespace, and the two failures observed live were exactly that:

  * the whole asset JSON was stored, so recall came back as bookkeeping
    ("asset ID: asset-analysis-…-v2, version 2, status: in-review"), and
  * the extraction model inferred things about the requester that nobody said
    ("the user's background (hands-on with AWS serverless, Lambda, DynamoDB)
    suggests …" for a request whose entire text was five words), which a later
    run then cited as the rationale for a recommendation.

So `_insight_from` condenses the asset to prose and strips sentences that are
about the requester or about this system's own run history.
"""

import pytest


@pytest.fixture()
def ctx():
    """An AgentContext with no __init__ — these two helpers only read `topic`."""
    from app.common.context import AgentContext
    c = AgentContext.__new__(AgentContext)
    c.topic = "Build an agentic ai application"
    return c


ASSET = {
    "assetId": "asset-analysis-build-an-agentic-ai-application-v2",
    "version": 2,
    "status": "in-review",
    "createdByAgent": "analysis",
    "title": "Agentic AI Application Build",
    "executiveSummary": "Framework choice is the load-bearing decision.",
    "openQuestions": ["What is the deployment target?", "Who owns the data?"],
}


def test_bookkeeping_fields_never_reach_memory(ctx):
    """The regression that made recall worthless: ids, versions and statuses were
    the most repeated strings in the stored blob, so that is what got extracted."""
    import json

    insight = ctx._insight_from(json.dumps(ASSET))
    for bookkeeping in ("assetId", "asset-analysis", "in-review", "version"):
        assert bookkeeping not in insight
    assert "Framework choice is the load-bearing decision." in insight


def test_the_topic_and_the_unknowns_are_what_gets_kept(ctx):
    """Recall is queried by the run's topic, so the topic has to be in the record
    for it to match; the unresolved questions are the reusable part."""
    import json

    insight = ctx._insight_from(json.dumps(ASSET))
    assert "Agentic AI Application Build" in insight
    assert "What is the deployment target?" in insight


def test_the_unknowns_are_found_in_a_customers_own_vocabulary(ctx):
    """The framework must not need to know your schema to remember the useful part.

    This used to look for `openQuestions`, `limitations` and `dataLimitations` — this
    sample's own field names — so a workflow that called the same thing something
    else stored the gist and silently dropped the unresolved half. Found by shape
    (a list of strings) and a generic uncertainty word instead.
    """
    import json

    for key in ("caveats", "openGaps", "unknowns", "outstandingItems",
                "blockers", "riskRegister"):
        insight = ctx._insight_from(json.dumps({
            "assetId": "asset-claim-decision-v1",
            "assetType": "claim-decision",
            "executiveSummary": "The claim is payable in part.",
            key: ["Policy wording for clause 4(b) is ambiguous."],
        }))
        assert "The claim is payable in part." in insight
        assert "Unresolved: Policy wording for clause 4(b) is ambiguous." in insight, key


def test_a_list_that_is_not_about_uncertainty_is_not_stored_as_unresolved(ctx):
    """The heuristic has to be narrow enough to stay meaningful: a list of parties
    or line items is not a list of open questions."""
    import json

    insight = ctx._insight_from(json.dumps({
        "assetId": "asset-claim-decision-v1",
        "executiveSummary": "The claim is payable in part.",
        "claimants": ["A. Patel", "R. Gomez"],
        "lineItems": ["Windscreen", "Courtesy car"],
    }))
    assert "Unresolved" not in insight
    assert "A. Patel" not in insight


def test_claims_about_the_requester_are_dropped(ctx):
    """Observed live, and the reason a later run recommended Lambda: memory
    asserted a background the user never mentioned."""
    import json

    insight = ctx._insight_from(json.dumps({
        "title": "T",
        "executiveSummary": (
            "Framework choice is the load-bearing decision. The user has "
            "hands-on experience with AWS serverless. Cost was not supplied."
        ),
    }))
    assert "load-bearing" in insight
    assert "Cost was not supplied." in insight
    assert "hands-on" not in insight


def test_run_counts_are_dropped_because_they_are_stale_tomorrow(ctx):
    """"…has completed four prior runs" was true when written and wrong on the
    next run. A record that ages badly should not be written at all."""
    import json

    insight = ctx._insight_from(json.dumps({
        "title": "T",
        "executiveSummary": "The user has completed four prior runs on this "
                            "subject. Framework choice dominates.",
    }))
    assert "four prior runs" not in insight
    assert "Framework choice dominates." in insight


def test_a_plain_text_agent_output_is_still_filtered_and_capped(ctx):
    """The non-JSON path stored content verbatim, so it bypassed the filter."""
    insight = ctx._insight_from(
        "The user prefers Terraform. " + "Grounded finding. " * 200)
    assert "prefers Terraform" not in insight
    assert len(insight) <= ctx._INSIGHT_MAX_CHARS


def test_an_ordinary_sentence_mentioning_a_user_survives(ctx):
    """The filter targets assertions ABOUT the requester, not the word 'user' —
    over-filtering would silently drop legitimate subject matter."""
    kept = ctx._drop_meta_sentences(
        "Each user session needs an isolated memory namespace.")
    assert kept == "Each user session needs an isolated memory namespace."


# ---------------------------------------------------------------------------
# Recall must not reach across unrelated subjects
# ---------------------------------------------------------------------------

TOPIC = "Design a serverless data pipeline on AWS"
# Verbatim from a live run's recall panel, on the pipeline request above. Both were
# stored by an EARLIER run about an agentic AI application.
BLED_THROUGH = [
    "The user is interested in building an agentic AI application.",
    ("The user is working on building an agentic AI application. An analysis "
     "identified nine core architectural components and five orchestration "
     "patterns required for the system."),
]


def test_an_insight_from_an_unrelated_subject_is_dropped():
    """Every request in the deployment shared one namespace per agent, so semantic
    search returned its top_k whether or not anything was close. The agents
    correctly refused to use these as rationale — the prompt forbids it — but the
    framework should not have offered them, and every synthesis agent paid tokens
    to read them."""
    from app.common.context import _on_topic
    assert _on_topic(BLED_THROUGH, TOPIC) == []


def test_an_insight_about_this_subject_survives():
    """The bar is one shared content word, so a near-miss on wording still recalls.
    A filter that dropped these would be worse than the leak it replaced."""
    from app.common.context import _on_topic
    on_topic = [
        ("Serverless data pipelines on AWS organise work into ingestion, storage, "
         "processing and consumption layers."),
        "A prior pipeline run found Glue ETL DPU-hours dominate the bill.",
    ]
    assert _on_topic(on_topic, TOPIC) == on_topic


def test_a_query_with_nothing_to_compare_keeps_everything():
    """No significant words means no evidence either way, and silently discarding
    a recall is the failure this rule is supposed to prevent."""
    from app.common.context import _on_topic
    assert _on_topic(BLED_THROUGH, "") == BLED_THROUGH
    assert _on_topic(BLED_THROUGH, "the it a") == BLED_THROUGH


def test_sharing_only_a_filler_word_is_not_sharing_a_subject():
    """"building", "request" and "user" appear in almost every insight this system
    stores, so matching on them would readmit everything the rule just excluded."""
    from app.common.context import _on_topic
    assert _on_topic(["The user is building something for a request."],
                     "Design a serverless data pipeline") == []


def test_the_namespace_is_scoped_by_topic_when_no_subject_is_set():
    """The defect was the FALLBACK: no subject_id meant a bare agent id, which is
    one shared bucket for every topic in the deployment."""
    from app.common.context import _slug
    a = _slug(TOPIC)[:48]
    b = _slug("Build a agentic ai application4")[:48]
    assert a and b and a != b, "two topics must not share one namespace"


def test_an_explicit_subject_still_wins():
    """`subject_id` is how an operator groups runs deliberately — a customer, an
    account — and deriving from the topic must not take that away."""
    from app.common.context import _slug
    assert _slug("ACME Corp") == "acme-corp"
