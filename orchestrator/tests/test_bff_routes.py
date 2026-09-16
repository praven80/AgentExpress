"""Every mutating BFF route is actually gated.

test_authz.py proves the RULES are right. This proves they are WIRED: a rule that
is correct but not applied on `/decision` is worth nothing, and "I added the guard
to five of the six routes" is exactly the mistake that would not show up anywhere
else until someone approved a gate they shouldn't have.

So each route is driven through `handler._api` with a real API Gateway event shape
and asserted twice — denied for a caller without the group, allowed for one with
it. DynamoDB and the runtime self-invoke are stubbed; nothing here touches AWS.
"""

import importlib
import json
import sys
import types

import pytest

RULES = {
    "groupsClaim": "cognito:groups",
    "actions": {
        "decision": ["approvers"],
        "rerun": ["approvers"],
        "cancel": ["approvers", "operators"],
        "evaluate": ["operators"],
        "insights": ["operators"],
        "delete": ["operators"],
    },
}

WORKFLOW = {"agents": {"a": {"name": "A"}, "b": {"name": "B"}},
            "steps": [{"agent": "a"}, {"agent": "b"}],
            "authorization": RULES}


class FakeTable:
    """Enough DynamoDB surface for the routes under test."""

    def __init__(self, item=None):
        self.item = item
        self.updates = []
        self.deletes = []

    def get_item(self, **kw):
        return {"Item": dict(self.item)} if self.item else {}

    def update_item(self, **kw):
        self.updates.append(kw)
        return {}

    def delete_item(self, **kw):
        self.deletes.append(kw)
        return {}

    def query(self, **kw):
        return {"Items": []}

    def scan(self, **kw):
        return {"Items": []}


@pytest.fixture()
def bff(monkeypatch):
    """bff/handler.py imported with the RBAC rules above and AWS stubbed out."""
    monkeypatch.setenv("WORKFLOW_JSON", json.dumps(WORKFLOW))
    monkeypatch.setenv("STATUS_TABLE", "t-status")
    monkeypatch.setenv("EVENTS_TABLE", "t-events")
    monkeypatch.setenv("RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-east-1:1:runtime/x")
    for mod in ("authz", "chatbot", "handler"):
        sys.modules.pop(mod, None)
    handler = importlib.import_module("handler")

    running = {"session_id": "s1", "overall": "running", "user": "someone@example.com"}
    status, events = FakeTable(running), FakeTable()
    monkeypatch.setattr(handler, "status_tbl", status)
    monkeypatch.setattr(handler, "events_tbl", events)
    invoked: list[dict] = []
    monkeypatch.setattr(handler, "_self_invoke",
                        lambda fn, payload: invoked.append(payload))
    handler._test_invoked = invoked
    handler._test_status = status
    yield handler
    for mod in ("authz", "chatbot", "handler"):
        sys.modules.pop(mod, None)


CTX = types.SimpleNamespace(function_name="bff-fn")


def call(bff, method, path, *, groups=None, body=None, params=None, route_key=""):
    claims = {"email": "user@example.com"}
    if groups is not None:
        claims["cognito:groups"] = groups
    event = {
        "requestContext": {"http": {"method": method},
                           "authorizer": {"jwt": {"claims": claims}}},
        "rawPath": path,
        "routeKey": route_key or f"{method} {path}",
        "pathParameters": params or {},
    }
    if body is not None:
        event["body"] = json.dumps(body)
    resp = bff._api(event, CTX)
    return resp["statusCode"], json.loads(resp["body"])


# Each entry: (action, method, path, pathParameters, body)
MUTATING_ROUTES = [
    ("decision", "POST", "/api/sessions/s1/decision", {"id": "s1"}, {"decision": "approve"}),
    ("rerun", "POST", "/api/sessions/s1/rerun", {"id": "s1"}, {"agentId": "a"}),
    ("cancel", "POST", "/api/sessions/s1/cancel", {"id": "s1"}, None),
    ("evaluate", "POST", "/api/sessions/s1/evaluate", {"id": "s1"}, {"agentId": "a"}),
    ("insights", "POST", "/api/insights/run", {}, {"lookbackHours": 24}),
    ("delete", "DELETE", "/api/sessions/s1", {"id": "s1"}, None),
]
IDS = [r[0] for r in MUTATING_ROUTES]


@pytest.mark.parametrize("action,method,path,params,body", MUTATING_ROUTES, ids=IDS)
def test_route_denies_a_caller_without_the_group(bff, action, method, path, params, body):
    status, resp = call(bff, method, path, groups=["interns"], body=body, params=params)
    assert status == 403, f"{action} was NOT gated"
    assert resp["error"] == f"not authorized to perform '{action}'"
    assert resp["requiredGroups"] == RULES["actions"][action]
    assert resp["yourGroups"] == ["interns"]


@pytest.mark.parametrize("action,method,path,params,body", MUTATING_ROUTES, ids=IDS)
def test_route_allows_a_caller_with_the_group(bff, action, method, path, params, body):
    group = RULES["actions"][action][0]
    status, _ = call(bff, method, path, groups=[group], body=body, params=params)
    assert status == 200, f"{action} was wrongly refused for group {group}"


@pytest.mark.parametrize("action,method,path,params,body", MUTATING_ROUTES, ids=IDS)
def test_a_denied_route_has_no_side_effect(bff, action, method, path, params, body):
    """A 403 must be refused BEFORE the work starts — not after the runtime has
    already been invoked or the row already written."""
    call(bff, method, path, groups=[], body=body, params=params)
    assert bff._test_invoked == []
    assert bff._test_status.updates == []
    assert bff._test_status.deletes == []


def test_read_routes_are_not_gated(bff):
    """They are already behind the JWT authorizer, and hiding a run from someone who
    can see the UI buys nothing."""
    assert call(bff, "GET", "/api/workflow", groups=[])[0] == 200
    assert call(bff, "GET", "/api/sessions", groups=[])[0] == 200
    assert call(bff, "GET", "/api/sessions/s1", groups=[], params={"id": "s1"})[0] == 200


def test_api_me_reports_what_this_caller_may_do(bff):
    status, body = call(bff, "GET", "/api/me", groups=["approvers"])
    assert status == 200
    assert body == {"user": "user@example.com", "groups": ["approvers"],
                    # `start` is not named in the fixture's authorization block, so it
                    # is unrestricted and every caller may do it.
                    "permittedActions": ["start", "decision", "rerun", "cancel"],
                    "authzEnabled": True}


def test_api_me_for_a_caller_with_nothing(bff):
    _, body = call(bff, "GET", "/api/me", groups=[])
    # Only the actions the config leaves unrestricted.
    assert body["permittedActions"] == ["start"]


def test_the_chat_route_is_told_what_the_caller_may_do(bff, monkeypatch):
    """The assistant is a second path to the same actions, so the permitted list has
    to reach it. Without this the RBAC would be enforced on the buttons only."""
    seen = {}

    def fake_handle_chat(body, ctx_session, self_invoke, permitted=None):
        seen["permitted"] = permitted
        return {"reply": "ok", "actions": []}

    monkeypatch.setattr(bff.chatbot, "is_enabled", lambda: True)
    monkeypatch.setattr(bff.chatbot, "handle_chat", fake_handle_chat)

    status, _ = call(bff, "POST", "/api/chat", groups=["operators"],
                     body={"message": "hi"})
    assert status == 200
    assert seen["permitted"] == ["start", "cancel", "evaluate", "insights", "delete"]


# --- with no authorization block, nothing is gated -------------------------

@pytest.fixture()
def open_bff(monkeypatch):
    monkeypatch.setenv("WORKFLOW_JSON", json.dumps(
        {k: v for k, v in WORKFLOW.items() if k != "authorization"}))
    monkeypatch.setenv("STATUS_TABLE", "t-status")
    monkeypatch.setenv("EVENTS_TABLE", "t-events")
    monkeypatch.setenv("RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-east-1:1:runtime/x")
    for mod in ("authz", "chatbot", "handler"):
        sys.modules.pop(mod, None)
    handler = importlib.import_module("handler")
    monkeypatch.setattr(handler, "status_tbl",
                        FakeTable({"session_id": "s1", "overall": "running"}))
    monkeypatch.setattr(handler, "events_tbl", FakeTable())
    monkeypatch.setattr(handler, "_self_invoke", lambda fn, payload: None)
    yield handler
    for mod in ("authz", "chatbot", "handler"):
        sys.modules.pop(mod, None)


@pytest.mark.parametrize("action,method,path,params,body", MUTATING_ROUTES, ids=IDS)
def test_without_rules_every_route_is_open(open_bff, action, method, path, params, body):
    """The pre-RBAC behaviour, preserved so removing the block is a safe way out."""
    status, _ = call(open_bff, method, path, groups=None, body=body, params=params)
    assert status == 200


def test_api_me_reports_authz_disabled(open_bff):
    _, body = call(open_bff, "GET", "/api/me", groups=None)
    assert body["authzEnabled"] is False
    assert body["permittedActions"] == [
        "start", "decision", "rerun", "cancel", "evaluate", "insights", "delete"]
