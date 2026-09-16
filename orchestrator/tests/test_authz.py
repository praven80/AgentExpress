"""RBAC on run actions (bff/authz.py) and its two enforcement points.

The rules come from `authorization` in workflow.json, so they are config-plane.
Three things are worth pinning:

  1. the SEMANTICS — absent means unrestricted, empty means denied to everyone.
     Getting that backwards either locks everyone out of a working deployment or
     silently grants the thing you thought you had closed.
  2. the CLAIM SHAPES — the groups claim arrives as a list, a JSON string, or
     Cognito's bracketed form depending on the path. All three must normalise.
  3. the ASSISTANT — it can approve gates and re-run agents, so if it were not
     held to the same rules, "ask the chatbot" would be a way around the buttons.

`authz` reads WORKFLOW_JSON at import, so each test re-imports it with the rules
it wants.
"""

import importlib
import json
import sys

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


def load_authz(authorization: dict | None, monkeypatch):
    """Import bff/authz.py fresh with the given `authorization` block."""
    payload = {} if authorization is None else {"authorization": authorization}
    monkeypatch.setenv("WORKFLOW_JSON", json.dumps(payload))
    sys.modules.pop("authz", None)
    module = importlib.import_module("authz")
    monkeypatch.delitem(sys.modules, "authz", raising=False)
    return module


def event(groups=None, claim="cognito:groups", **claims):
    """An API Gateway HTTP API event with JWT claims attached."""
    c = dict(claims)
    if groups is not None:
        c[claim] = groups
    return {"requestContext": {"authorizer": {"jwt": {"claims": c}}}}


# --- semantics -------------------------------------------------------------

def test_no_authorization_block_leaves_everything_open(monkeypatch):
    """Backwards compatibility is the whole reason for this default: deleting the
    block must restore the pre-RBAC behaviour rather than deny everything."""
    authz = load_authz(None, monkeypatch)
    assert authz.ENABLED is False
    for action in authz.ACTIONS:
        assert authz.permitted(action, event()) is True


def test_an_action_not_listed_is_unrestricted(monkeypatch):
    authz = load_authz({"actions": {"decision": ["approvers"]}}, monkeypatch)
    assert authz.permitted("decision", event(["nobody"])) is False
    assert authz.permitted("delete", event(["nobody"])) is True   # not listed


def test_an_action_listed_with_an_empty_list_is_denied_to_everyone(monkeypatch):
    """The documented way to switch a capability off. It must NOT read as
    "unrestricted"."""
    authz = load_authz({"actions": {"delete": []}}, monkeypatch)
    assert authz.permitted("delete", event(["approvers", "operators"])) is False
    assert authz.permitted("delete", event()) is False


def test_holding_any_one_of_the_listed_groups_is_enough(monkeypatch):
    authz = load_authz(RULES, monkeypatch)
    assert authz.permitted("cancel", event(["operators"])) is True
    assert authz.permitted("cancel", event(["approvers"])) is True


def test_a_user_with_no_groups_can_do_nothing_restricted(monkeypatch):
    """"Nothing RESTRICTED" — an action the config does not name stays open, which is
    the documented backwards-compatible default."""
    authz = load_authz(RULES, monkeypatch)
    unrestricted = [a for a in authz.ACTIONS if a not in RULES["actions"]]
    assert authz.permitted_actions(event([])) == unrestricted
    assert authz.permitted_actions(event()) == unrestricted
    for action in RULES["actions"]:
        assert authz.permitted(action, event([])) is False
        assert authz.permitted(action, event()) is False


def test_permitted_actions_per_role(monkeypatch):
    """Derived, not restated: an action the RULES fixture does not name is
    UNRESTRICTED, so it is permitted to everyone and appears in every list. Computing
    the expectation keeps this honest when a new action is added."""
    authz = load_authz(RULES, monkeypatch)
    unrestricted = [a for a in authz.ACTIONS if a not in RULES["actions"]]

    def expected(*groups):
        allowed = {a for a, gs in RULES["actions"].items() if set(gs) & set(groups)}
        return [a for a in authz.ACTIONS if a in allowed or a in unrestricted]

    assert authz.permitted_actions(event(["approvers"])) == expected("approvers")
    assert authz.permitted_actions(event(["operators"])) == expected("operators")
    assert authz.permitted_actions(event(["approvers", "operators"])) == list(authz.ACTIONS)
    # And the fixture really does restrict something, or the test proves nothing.
    assert expected("approvers") != list(authz.ACTIONS)


def test_an_unrecognised_group_grants_only_the_unrestricted_actions(monkeypatch):
    authz = load_authz(RULES, monkeypatch)
    unrestricted = [a for a in authz.ACTIONS if a not in RULES["actions"]]
    assert authz.permitted_actions(event(["interns"])) == unrestricted
    # Every action the config DOES restrict is refused.
    for action in RULES["actions"]:
        assert authz.permitted(action, event(["interns"])) is False


def test_the_recognised_actions_are_exactly_the_ones_the_iac_validates(monkeypatch):
    """Both IaC paths hardcode this list to reject a typo'd action key. If a new
    action is added here, those two lists must be updated too."""
    authz = load_authz(None, monkeypatch)
    assert sorted(authz.ACTIONS) == [
        "cancel", "decision", "delete", "evaluate", "insights", "rerun", "start"]


# --- claim shapes ----------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    (["approvers", "operators"], ["approvers", "operators"]),   # real array
    ('["approvers","operators"]', ["approvers", "operators"]),   # JSON string
    ("[approvers operators]", ["approvers", "operators"]),       # Cognito bracketed
    ("approvers,operators", ["approvers", "operators"]),         # comma separated
    ("approvers operators", ["approvers", "operators"]),         # space separated
    ("approvers", ["approvers"]),                                # single value
    ("", []),
    ("   ", []),
    ([], []),
    (["approvers", "", "  "], ["approvers"]),                    # blanks dropped
])
def test_group_claim_shapes_all_normalise(raw, expected, monkeypatch):
    """API Gateway and the two providers are not consistent about this, so the
    shape is normalised rather than assumed."""
    authz = load_authz(RULES, monkeypatch)
    assert authz.groups_of(event(raw)) == expected


def test_a_missing_claim_is_not_an_error(monkeypatch):
    authz = load_authz(RULES, monkeypatch)
    assert authz.groups_of(event()) == []
    assert authz.groups_of({}) == []                     # no requestContext at all


def test_the_groups_claim_name_is_configurable_for_auth0(monkeypatch):
    """Auth0 will not issue an unnamespaced custom claim, so the claim name has to
    be config rather than the Cognito default."""
    authz = load_authz({"groupsClaim": "https://your-app/roles",
                        "actions": {"decision": ["approver"]}}, monkeypatch)
    assert authz.GROUPS_CLAIM == "https://your-app/roles"
    assert authz.permitted("decision", event(["approver"], claim="https://your-app/roles"))
    # The Cognito claim is ignored when a different one is configured.
    assert not authz.permitted("decision", event(["approver"], claim="cognito:groups"))


# --- the 403 body ----------------------------------------------------------

def test_the_denial_body_is_diagnosable_on_its_own(monkeypatch):
    """A denial is nearly always a group-mapping mistake, so the response says what
    was required, what the caller had, and which claim was read."""
    authz = load_authz(RULES, monkeypatch)
    body = authz.denial("decision", event(["interns"]))
    assert body == {"error": "not authorized to perform 'decision'",
                    "requiredGroups": ["approvers"],
                    "yourGroups": ["interns"],
                    "groupsClaim": "cognito:groups"}


def test_the_denial_body_for_a_closed_action_shows_no_required_groups(monkeypatch):
    authz = load_authz({"actions": {"delete": []}}, monkeypatch)
    assert authz.denial("delete", event(["operators"]))["requiredGroups"] == []


# --- the shipped config ----------------------------------------------------

def test_the_shipped_authorization_block_is_coherent(shipped, monkeypatch):
    """Every action it names must be one authz recognises, and every group must
    grant at least one action (an unused group would be created in Cognito for
    nothing)."""
    block = shipped["authorization"]
    authz = load_authz(block, monkeypatch)
    assert set(block["actions"]) <= set(authz.ACTIONS)
    named = {g for gs in block["actions"].values() for g in gs}
    for g in named:
        assert authz.permitted_actions(event([g])), f"group {g} grants nothing"


# --- the assistant is held to the same rules ------------------------------

def load_chatbot(monkeypatch, tools=None):
    monkeypatch.setenv("WORKFLOW_JSON", json.dumps(
        {"chatbot": {"enabled": True, "tools": tools or {}}, "agents": {}}))
    sys.modules.pop("chatbot", None)
    module = importlib.import_module("chatbot")
    monkeypatch.delitem(sys.modules, "chatbot", raising=False)
    return module


ACTION_TOOLS = {"submit_review", "rerun_agents", "run_evaluation"}


def enabled_names(chatbot, permitted):
    return {v["name"] for v in chatbot._enabled_tools(permitted).values()}


def test_the_assistant_keeps_every_tool_when_there_is_no_authz_context(monkeypatch):
    chatbot = load_chatbot(monkeypatch)
    assert ACTION_TOOLS <= enabled_names(chatbot, None)


def test_an_action_tool_is_withheld_when_the_caller_lacks_the_group(monkeypatch):
    """Withholding beats refusing in the prompt: a tool the model was never given
    cannot be talked into being called."""
    chatbot = load_chatbot(monkeypatch)
    names = enabled_names(chatbot, ["start", "cancel", "evaluate", "insights", "delete"])
    assert "run_evaluation" in names          # operators may evaluate
    assert "submit_review" not in names       # but not approve
    assert "rerun_agents" not in names


def test_a_caller_with_no_permissions_gets_only_read_tools(monkeypatch):
    chatbot = load_chatbot(monkeypatch)
    names = enabled_names(chatbot, [])
    assert not (ACTION_TOOLS & names)
    assert "get_status" in names              # reads are not gated


def test_config_disabled_tools_are_dropped_independently_of_authz(monkeypatch):
    """Two separate filters: what the deployment offers at all, and what this
    caller may do."""
    chatbot = load_chatbot(monkeypatch, tools={"costs": False, "review": False})
    names = enabled_names(chatbot, list(RULES["actions"]))
    assert "get_costs" not in names
    assert "submit_review" not in names
    assert "rerun_agents" in names            # still permitted and still enabled


def test_every_action_tool_declares_which_authz_action_it_needs(monkeypatch):
    """A new action tool without an `authz` key would be ungated — this is the test
    that catches it."""
    chatbot = load_chatbot(monkeypatch)
    for key, spec in chatbot._REGISTRY.items():
        if spec.get("action"):
            assert spec.get("authz"), f"action tool {key} has no authz mapping"


def test_authz_mappings_point_at_real_actions(monkeypatch):
    authz = load_authz(None, monkeypatch)
    chatbot = load_chatbot(monkeypatch)
    for spec in chatbot._REGISTRY.values():
        if spec.get("authz"):
            assert spec["authz"] in authz.ACTIONS


def test_the_prompt_explains_a_withheld_capability(monkeypatch):
    """Not a control — the withholding is. This only stops the assistant guessing
    at why it cannot act, so the user hears "you lack permission" rather than an
    unexplained refusal."""
    chatbot = load_chatbot(monkeypatch)
    rule = chatbot._authz_rule(["cancel"])
    assert "NOT authorized" in rule
    assert "approve, revise or deny a review gate" in rule
    assert "Never claim you did it" in rule


def test_no_prompt_line_is_added_when_nothing_is_withheld(monkeypatch):
    chatbot = load_chatbot(monkeypatch)
    assert chatbot._authz_rule(list(RULES["actions"])) == ""
    assert chatbot._authz_rule(None) == ""


def test_a_capability_switched_off_in_config_is_not_reported_as_a_permission_problem(
        monkeypatch):
    chatbot = load_chatbot(monkeypatch, tools={"review": False, "rerun": False,
                                               "runEval": False})
    assert chatbot._authz_rule([]) == ""
