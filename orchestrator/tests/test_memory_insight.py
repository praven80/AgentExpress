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
