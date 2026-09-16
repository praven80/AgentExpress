"""The built-in demo tool function (orchestrator/tool_lambda/handler.py).

The bug these exist for: `prior_runs` scanned the status table with no exclusion
for the calling session, so the agent was handed its OWN run as a "prior run". In a
real run it reported

    "Run 7e7380d13b07 is currently running ... started 6 seconds before this
     request brief was created"

and the pipeline then produced "Coordinate with concurrent run 7e7380d13b07 to
avoid duplicate effort" as the #1 high-priority recommendation in the final report.
The system advised the reader to coordinate with itself.
"""

import importlib
import sys

import pytest


class FakeTable:
    """Enough DynamoDB surface for the handler, including forced pagination."""

    def __init__(self, items, page=100):
        self.items = items
        self.page = page

    def scan(self, **kw):
        start = kw.get("ExclusiveStartKey") or 0
        chunk = self.items[start:start + self.page]
        out = {"Items": chunk}
        if start + self.page < len(self.items):
            out["LastEvaluatedKey"] = start + self.page
        return out

    def query(self, **kw):
        return {"Items": []}


RUNS = [
    {"session_id": "current0001", "topic": "How to build an Agentic AI application",
     "overall": "running", "created": "2026-09-16 10:21:49"},
    {"session_id": "done0000001", "topic": "How to build an Agentic AI application",
     "overall": "done", "created": "2026-09-16 09:00:00"},
    {"session_id": "denied00001", "topic": "Agentic AI application rollout",
     "overall": "denied", "created": "2026-09-16 08:00:00"},
    {"session_id": "paused00001", "topic": "How to build an Agentic AI application",
     "overall": "waiting_human", "created": "2026-09-16 07:00:00"},
    {"session_id": "cancelling1", "topic": "How to build an Agentic AI application",
     "overall": "cancelling", "created": "2026-09-16 06:00:00"},
    {"session_id": "other000001", "topic": "Design a serverless data pipeline on AWS",
     "overall": "done", "created": "2026-09-16 05:00:00"},
]


@pytest.fixture()
def tl(monkeypatch):
    monkeypatch.setenv("STATUS_TABLE", "t-status")
    monkeypatch.setenv("TELEMETRY_TABLE", "t-telemetry")
    sys.path.insert(0, str(__import__("conftest").ORCH_ROOT / "tool_lambda"))
    sys.modules.pop("handler", None)
    mod = importlib.import_module("handler")
    monkeypatch.setattr(mod, "_status", FakeTable(RUNS))
    monkeypatch.setattr(mod, "_telemetry", FakeTable([]))
    yield mod
    sys.modules.pop("handler", None)


def ids(res) -> list[str]:
    return [r["sessionId"] for r in res["results"]]


def test_the_calling_run_is_never_returned_as_a_prior_run(tl):
    """It is always still `running`, which is what makes this filter sufficient
    without plumbing the session id through the Gateway."""
    out = tl.prior_runs({"topic": "agentic ai application", "limit": 25})
    assert "current0001" not in ids(out)


def test_in_flight_runs_are_excluded_and_the_count_is_reported(tl):
    """Excluded, not hidden — the agent can see runs were dropped and why."""
    out = tl.prior_runs({"topic": "agentic ai application", "limit": 25})
    assert "cancelling1" not in ids(out)
    assert out["inFlightRunsExcluded"] == 2      # the caller + the cancelling one
    assert out["completedRunsInSystem"] == 4


def test_settled_runs_are_returned_with_their_outcome(tl):
    out = tl.prior_runs({"topic": "agentic ai application", "limit": 25})
    got = {r["sessionId"]: r["outcome"] for r in out["results"]}
    assert got["done0000001"] == "done"
    assert got["denied00001"] == "denied"


def test_a_paused_run_is_kept(tl):
    """`waiting_human` is a real, useful state — someone is mid-review on this
    topic — and it cannot be the caller, because the caller is executing."""
    out = tl.prior_runs({"topic": "agentic ai application", "limit": 25})
    assert "paused00001" in ids(out)


def test_results_are_newest_first(tl):
    out = tl.prior_runs({"topic": "agentic ai", "limit": 25})
    created = [r["text"] for r in out["results"]]
    assert "done0000001" in created[0]


def test_limit_is_honoured_and_capped(tl):
    assert len(tl.prior_runs({"topic": "agentic", "limit": 2})["results"]) == 2
    assert len(tl.prior_runs({"topic": "agentic", "limit": 999})["results"]) <= tl.MAX_RUNS
    # A non-numeric limit must not raise.
    assert tl.prior_runs({"topic": "agentic", "limit": "lots"})["results"]


def test_an_unmatched_topic_returns_nothing_rather_than_everything(tl):
    out = tl.prior_runs({"topic": "quantum cryptography supply chain", "limit": 25})
    assert out["results"] == []
    # And it still says how many settled runs exist, so "no history" is
    # distinguishable from "no matching history".
    assert out["completedRunsInSystem"] == 4


def test_scan_is_paginated(tl, monkeypatch):
    """A Scan returns at most 1MB per page and status items are large, so without
    following LastEvaluatedKey a history tool quietly answers "no prior runs"."""
    monkeypatch.setattr(tl, "_status", FakeTable(RUNS, page=1))
    out = tl.prior_runs({"topic": "agentic ai application", "limit": 25})
    assert out["completedRunsInSystem"] == 4


# --- dispatch --------------------------------------------------------------

class Ctx:
    def __init__(self, tool):
        self.client_context = type("C", (), {"custom": {"bedrockAgentCoreToolName": tool}})()


def test_the_tool_name_selects_the_implementation(tl):
    out = tl.lambda_handler({"topic": "agentic"}, Ctx("runs___prior_runs"))
    assert "results" in out


def test_a_doubly_prefixed_tool_name_still_resolves(tl):
    out = tl.lambda_handler({"topic": "agentic"}, Ctx("gw___runs___prior_runs"))
    assert "results" in out


def test_an_unknown_tool_is_named_rather_than_guessed(tl):
    """Silently running the wrong tool would return real-looking data for a
    question nobody asked."""
    out = tl.lambda_handler({}, Ctx("runs___nope"))
    assert "unknown tool" in out["error"]
    assert sorted(out["availableTools"]) == ["prior_runs", "run_costs"]


def test_run_costs_requires_a_session_id(tl):
    out = tl.lambda_handler({}, Ctx("runs___run_costs"))
    assert "session_id" in out["error"]
