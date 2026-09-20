"""The Lifecycle Research agent (app/subagents/lifecycle_research/), the `type: "openapi"`
demonstration — and specifically the two ways its date reasoning was WRONG when it first
ran live against the real API.

THE FIXTURES BELOW ARE THE REAL RESPONSE SHAPE, taken from the live catalogue. That
matters more than it sounds, because both defects were invisible to any fixture invented
from the schema:

  1. THE CATALOGUE RETURNS RELEASES NEWEST FIRST. The agent assumed oldest first and
     reversed the list, which inverted every ordered claim it made. "The newest supported
     releases" named the OLDEST four, and "the most recent release to reach end of life"
     named the oldest one on record — Python 2.6, ended 2013. Both read as confident
     fact. Ordering taken from a response's shape is an assumption about someone else's
     API; ordering taken from a field in it is a fact.

  2. `isMaintained` DOES NOT MEAN "STILL SUPPORTED". The catalogue reports Node 12 as
     `isMaintained: true` AND `isEol: true` — it marks membership of the maintenance
     TRACK, not current support. Reading it as current support produced an asset claiming
     eight supported Node releases and offering 2022-04-30, nearly four years past, as
     "the earliest planning deadline this dependency set implies".

Neither was caught by a type error, a contract violation or an empty result. The asset
was well-formed, every date in it was real, and the sentences built out of them were
false. That is the failure mode this file exists to pin.
"""
from __future__ import annotations

import importlib
import json

import pytest
from conftest import needs_agent, needs_tool

pytestmark = [needs_agent("lifecycle_research"), needs_tool("lifecycle")]


@pytest.fixture
def mod():
    # importlib, NOT `from app.subagents.lifecycle_research import agent` — the package's
    # __init__ re-exports `agent` as the Agent INSTANCE (that is the folder contract), so
    # the plain import binds the object and every helper lookup on it fails.
    return importlib.import_module("app.subagents.lifecycle_research.agent")


# The `rowFields` mapping from the shipped workflow.json, so these tests exercise the
# same role->field translation a deployment does rather than a hand-written copy.
@pytest.fixture
def field_map():
    from conftest import ORCH_ROOT
    wf = json.loads((ORCH_ROOT / "app" / "workflow.json").read_text())
    return dict(wf["tools"]["lifecycle"]["rowFields"])


# Real Node.js records, newest first, exactly as the catalogue returns them. Note 12
# through 20: isMaintained true, isEol ALSO true.
NODEJS = [
    {"name": "26", "releaseDate": "2026-05-05", "isLts": False, "isEol": False,
     "eolFrom": "2029-04-30", "isMaintained": True, "eoasFrom": "2028-10-20"},
    {"name": "25", "releaseDate": "2025-10-15", "isLts": False, "isEol": True,
     "eolFrom": "2026-06-01", "isMaintained": False, "eoasFrom": "2026-04-01"},
    {"name": "24", "releaseDate": "2025-05-06", "isLts": True, "isEol": False,
     "eolFrom": "2028-04-30", "isMaintained": True, "eoasFrom": "2026-10-20"},
    {"name": "22", "releaseDate": "2024-04-24", "isLts": True, "isEol": False,
     "eolFrom": "2027-04-30", "isMaintained": True, "eoasFrom": "2025-10-21"},
    {"name": "20", "releaseDate": "2023-04-18", "isLts": True, "isEol": True,
     "eolFrom": "2026-04-30", "isMaintained": True, "eoasFrom": "2024-10-22"},
    {"name": "12", "releaseDate": "2019-04-23", "isLts": True, "isEol": True,
     "eolFrom": "2022-04-30", "isMaintained": True, "eoasFrom": "2020-11-30"},
]


def rows(mod, field_map, raw):
    return mod._rows(raw, field_map)


# ---------------------------------------------------------------------------
# Defect 1: ordering
# ---------------------------------------------------------------------------

def test_releases_are_ordered_by_their_published_date_not_by_response_order(mod, field_map):
    """The fix for the inversion: order comes from `releaseDate`, not from position."""
    got = [r["version"] for r in mod._newest_first(rows(mod, field_map, NODEJS))]
    assert got == ["26", "25", "24", "22", "20", "12"]
    # And it must still be right when the API hands them back the other way round,
    # which is the whole point of sorting rather than reversing.
    got_reversed = [r["version"] for r in
                    mod._newest_first(rows(mod, field_map, list(reversed(NODEJS))))]
    assert got_reversed == got


def test_a_release_with_no_date_sorts_last_rather_than_leading(mod, field_map):
    """Some products carry releases with a null date. Sorting must not crash, and must
    not let the undated one claim to be newest."""
    raw = [*NODEJS, {"name": "x", "releaseDate": None, "isEol": False, "eolFrom": None,
                     "isLts": False, "isMaintained": True, "eoasFrom": None}]
    assert mod._newest_first(rows(mod, field_map, raw))[0]["version"] == "26"


def test_the_most_recent_ended_release_is_the_newest_one_that_ended(mod, field_map):
    """Was Python 2.6 (1990s-era, oldest on record) because of the reversal."""
    finding = next(f for f in mod._findings("nodejs", rows(mod, field_map, NODEJS))
                   if "already reached" in f.statement)
    # 25 (2025-10-15) is the newest release flagged isEol among these.
    assert "25" in finding.statement
    assert "12" not in finding.statement.split("being")[1]


# ---------------------------------------------------------------------------
# Defect 2: which field decides "still supported"
# ---------------------------------------------------------------------------

def test_support_is_decided_by_iseol_and_not_by_ismaintained(mod, field_map):
    """Node 12/20 are isMaintained AND isEol. Only three of these six are supported."""
    supported = next(f for f in mod._findings("nodejs", rows(mod, field_map, NODEJS))
                     if "still supported" in f.statement)
    assert "3 release(s) still supported" in supported.statement, supported.statement
    # The ones whose support has ended must not be named as supported.
    for ended in ("12 (LTS) supported until", "20 (LTS) supported until"):
        assert ended not in supported.statement


def test_ismaintained_is_not_even_available_to_the_agent(field_map):
    """Removed from `rowFields` rather than merely unread. A field whose NAME answers the
    question being asked and whose VALUE does not is a trap for the next reader, so the
    config does not offer it."""
    assert "isMaintained" not in field_map
    assert field_map["isEol"] == "isEol"


def test_the_earliest_deadline_is_never_a_date_that_has_already_passed(mod, field_map):
    """The headline claim, and the one the bug made absurd: it offered 2022-04-30 as the
    next planning deadline. Now taken only from releases the catalogue says are NOT past
    end of life, so the earliest here is Node 22's 2027-04-30."""
    soonest = mod._soonest({"nodejs": rows(mod, field_map, NODEJS)})
    assert soonest is not None
    assert "2027-04-30" in soonest.statement
    assert "2022-04-30" not in soonest.statement


def test_no_deadline_is_claimed_from_a_single_release(mod, field_map):
    """"The first to lose support" is only meaningful as a comparison. With one dated
    supported release there is nothing to compare, and the per-product finding already
    states its date."""
    one = [r for r in NODEJS if r["name"] == "26"]
    assert mod._soonest({"nodejs": rows(mod, field_map, one)}) is None


# ---------------------------------------------------------------------------
# The discipline the agent exists to enforce
# ---------------------------------------------------------------------------

def test_every_finding_is_a_sourced_fact_attributed_to_the_product(mod, field_map):
    """This agent generates no interpretation. Each statement is the API's data
    rearranged, so anything not classified `sourced-fact` would be a claim it cannot
    support."""
    for finding in mod._findings("nodejs", rows(mod, field_map, NODEJS)):
        assert finding.classification == "sourced-fact"
        assert finding.source_ref == "Product lifecycle API, nodejs"


def test_an_unannounced_end_date_is_said_rather_than_blanked(mod, field_map):
    """A current release often has no announced end of life, and that is genuinely
    different from a known far-off one. Blanking it would read as a missing lookup."""
    raw = [{"name": "3", "releaseDate": "2026-01-01", "isEol": False, "eolFrom": None,
            "isLts": False, "isMaintained": True, "eoasFrom": None}]
    finding = mod._findings("kafka", rows(mod, field_map, raw))[0]
    assert "no announced date" in finding.statement


def test_products_the_catalogue_does_not_know_are_named_not_dropped(mod, field_map):
    """An openapi target is reached BY PATH, so a wrong identifier is a 404 rather than an
    empty result. Silently dropping it would leave the asset looking complete."""
    assert mod._rows([], field_map) == []
    # The identifier normaliser is what turns most near-misses into hits.
    assert mod._products({"products": ["Amazon Linux", "PostgreSQL", "  nodejs  "]}) == [
        "amazon-linux", "postgresql", "nodejs"]


def test_the_product_list_is_capped_and_deduplicated(mod):
    """Each identifier is one HTTP request through the Gateway, so a scattergun answer
    must not become a 30-call fan-out."""
    assert mod._products({"products": ["python"] * 5}) == ["python"]
    assert len(mod._products({"products": [f"p{i}" for i in range(30)]})) == mod.MAX_PRODUCTS


def test_a_path_traversal_identifier_is_refused(mod):
    """The value becomes a path segment on somebody else's API. The model is not a
    trusted source of path components."""
    assert mod._products({"products": ["../../etc/passwd", "a/b", "ok"]}) == ["ok"]
