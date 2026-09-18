"""Central configuration.

The workflow (agents + topology + HITL gates) is defined once in workflow.json
and loaded here. Everything else (graph, UI, BFF) derives from it, so adding an
agent is a config change plus a new agent module — no changes to the framework.

Load order for the workflow definition:
  1. WORKFLOW_JSON env var (inline JSON) — used by the Lambda BFF.
  2. app/workflow.json on disk — used by the runtime and local dev.
"""

import json
import os
from pathlib import Path

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "workflow.json"


def load_workflow() -> dict:
    inline = os.getenv("WORKFLOW_JSON")
    if inline:
        return json.loads(inline)
    return json.loads(_CONFIG_PATH.read_text())


WORKFLOW = load_workflow()
AGENTS: dict = WORKFLOW["agents"]
STEPS: list = WORKFLOW["steps"]
NODE_IDS: list = list(AGENTS.keys())

# The final agent in the pipeline (used to surface the run's result). The last
# step may be a single agent, a parallel group, or a sequence group.
_LAST_STEP = STEPS[-1]
LAST_AGENT_ID = _LAST_STEP.get("agent") or (
    _LAST_STEP.get("parallel") or _LAST_STEP.get("sequence"))[-1]

# The FIRST agent in the pipeline. Its output is the run's authoritative brief:
# every downstream agent reads it for the objective/title that frames the work
# (see app/common/research.py and app/common/synthesis.py).
#
# Derived from the topology rather than hardcoded, so the first agent can be
# called anything. It used to be the literal "intake", which meant renaming that
# agent silently dropped the brief everywhere instead of failing.
_FIRST_STEP = STEPS[0]
FIRST_AGENT_ID = _FIRST_STEP.get("agent") or (
    _FIRST_STEP.get("parallel") or _FIRST_STEP.get("sequence"))[0]

# Engine-level settings that describe the orchestrator itself (not an agent).
# The app reads 'defaultModel' and 'ui'; the IaC reads the rest (policy, chatbot,
# guardrail). Environment variables still take precedence at deploy time.
ORCHESTRATOR: dict = WORKFLOW.get("orchestrator", {})

# Output-rule enforcement (app/common/rules.py + app/common/structured.py), and
# specifically WHETHER A FAILED CHECK COSTS A SECOND MODEL CALL.
#
# The checks themselves are pure functions — no model, no IO, no measurable cost.
# The repair is a whole extra call per affected agent, which on an eight-agent
# workflow can double the price of a run. That is the customer's decision to make,
# not the framework's, so it is config with a per-agent override:
#
#   orchestrator.outputRules   { "enabled": true, "repair": false }   engine default
#   agents.<id>.outputRules    { "repair": true }                     per agent
#
# Three meaningful settings:
#   enabled false            no checks. One call. Nothing recorded.
#   enabled, repair false    check, record what failed on the asset, DO NOT re-ask.
#                            One call. The reviewer still sees every violation in
#                            the UI; they just fix it by revising the gate rather
#                            than paying the model to try again.
#   enabled, repair true     check, re-ask once with the violations quoted. Up to
#                            two calls for an agent that failed, one for an agent
#                            that passed.
#
# The default is repair OFF. Enforcement should not silently change what a run
# costs: a customer who deploys this sample gets the same bill as before the rules
# existed, plus visibility, and opts in to paying for repair per agent once they
# have seen which agents actually need it.
#   maxWords  a prose budget for one agent's payload, checked deterministically.
#             0 disables it, which is the engine default: a framework must not
#             impose a house style on a workflow it knows nothing about. Set it per
#             agent for the outputs where length is the defect — three separate
#             prompt clauses failed to keep one report under control, and a number
#             in config succeeds where wording did not because it is not
#             negotiable.
_OUTPUT_RULES_DEFAULTS = {"enabled": True, "repair": False, "maxWords": 0}
OUTPUT_RULES: dict = {**_OUTPUT_RULES_DEFAULTS,
                      **(ORCHESTRATOR.get("outputRules") or {})}

# How the orchestrator calls a DEDICATED agent runtime (app/common/agentcore_agent.py),
# and specifically WHETHER THE SDK MAY SILENTLY RUN AN AGENT TWICE.
#
# InvokeAgentRuntime is synchronous, slow (a research agent takes ~15-20s) and NOT
# idempotent: the remote container starts working the moment the request lands, and
# it has no idea whether the caller is still listening. boto3's default client
# enables retries — `retries={'mode': 'legacy'}`, up to 5 attempts — so one
# transient connection blip mid-call makes the SDK re-issue the request, the remote
# agent runs the WHOLE thing again, and the orchestrator returns whichever attempt
# answered last. The first attempt's model call is still billed, and nothing logs it:
# the node logs "Invoking dedicated AgentCore Runtime" once, before the retry exists.
#
# That is not hypothetical. On a live run the web_search runtime logged two
# invocations with different requestIds, 13s apart, for one node execution:
#   13:55:29  Invocation completed successfully (17.792s)  req 95297bd8...
#   13:55:41  Invocation completed successfully (16.669s)  req 26ad54ce...
# Two identical model calls, 6579 input tokens each, one result discarded, $0.0158
# of the run's $0.2448 spent on an answer nobody read.
#
# So retries are OFF by default here (maxAttempts 1 = one attempt, no retry) and the
# read timeout is generous instead. For a call this long the failure that matters is
# a LOST RESPONSE to work that already succeeded, and retrying that is strictly
# worse than failing: a raised error surfaces to the reviewer, whereas a duplicate
# silently double-bills. The framework already prefers a visible failure over a
# quiet degradation everywhere else (see app/common/errors.py).
#
#   maxAttempts         total attempts per invoke, not retries-after-the-first.
#                       1 disables retrying. Raise it only if you have made the
#                       call idempotent.
#   readTimeoutSeconds  how long to wait for the agent's response. Must exceed the
#                       slowest agent's wall-clock or you will time out mid-answer
#                       and, with maxAttempts 1, lose the run.
_RUNTIME_INVOKE_DEFAULTS = {"maxAttempts": 1, "readTimeoutSeconds": 120}
RUNTIME_INVOKE: dict = {**_RUNTIME_INVOKE_DEFAULTS,
                        **(ORCHESTRATOR.get("runtimeInvoke") or {})}


def output_rules_for(spec: dict) -> dict:
    """The engine default overlaid with one agent's `outputRules` block.

    Merged rather than replaced, so an agent that only wants to turn repair on
    writes `{"repair": true}` and does not have to restate `enabled`.
    """
    return {**OUTPUT_RULES, **(spec.get("outputRules") or {})}


# Presentation strings (title, the default topic, placeholders). Config rather
# than literals so re-branding for a different use case is a workflow.json edit.
UI: dict = WORKFLOW.get("ui", {})
DEFAULT_TOPIC: str = str(UI.get("defaultTopic") or "")

# --- runtime / infra settings (env-driven, then orchestrator block) -------

MODEL_ID = (os.getenv("BEDROCK_MODEL_ID")
            or ORCHESTRATOR.get("defaultModel")
            or "us.anthropic.claude-haiku-4-5-20251001-v1:0")
REGION = os.getenv("AWS_REGION", "us-east-1")
MEMORY_ID = os.getenv("MEMORY_ID")
STATUS_TABLE = os.getenv("STATUS_TABLE")
EVENTS_TABLE = os.getenv("EVENTS_TABLE")

MCP_TIMEOUT = float(os.getenv("MCP_TIMEOUT", "25"))

# AgentCore Gateway (the tool plane). Agents reach every tool through one
# JWT-authed Gateway endpoint; the runtime fetches a client-credentials token.
# An agent names a tool with its `tool` field in workflow.json, matching a KEY in
# the `tools` block, and the Gateway routes to that target. Swapping a target
# (a different MCP server, a REST API, your own Lambda) is a workflow.json edit.
GATEWAY_URL = os.getenv("GATEWAY_URL", "")
GATEWAY_TOKEN_URL = os.getenv("GATEWAY_TOKEN_URL", "")
GATEWAY_CLIENT_ID = os.getenv("GATEWAY_CLIENT_ID", "")
GATEWAY_CLIENT_SECRET = os.getenv("GATEWAY_CLIENT_SECRET", "")
# The OAuth2 `scope` (Cognito) or `audience` (Auth0) requested for the token.
GATEWAY_AUDIENCE = os.getenv("GATEWAY_AUDIENCE", "")
# Which client-credentials request shape to build: "cognito" | "auth0".
# Set by Terraform from the `idp` variable (see terraform/identity.tf). Defaults
# to cognito so an older deployment keeps working.
GATEWAY_AUTH_FLOW = os.getenv("GATEWAY_AUTH_FLOW", "cognito").lower()

def step_agents(step: dict) -> list[str]:
    """The agent ids in one `steps` entry, whatever its shape."""
    if "agent" in step:
        return [step["agent"]]
    return list(step.get("parallel") or step.get("sequence") or [])


def upstream_of(agent_id: str) -> list[str]:
    """Agent ids that run BEFORE `agent_id`, in reverse topology order.

    Derived from the `steps` topology so a downstream agent automatically reads
    every new upstream agent: add a research agent to the parallel group and the
    analysis agent picks it up with no code change. Reverse order puts the most
    recent (most specific) assets first, which is how the synthesis agents want
    their context.

    Within the agent's OWN step, membership depends on the step kind:
      * "parallel" — the others are peers running concurrently, so they are NOT
        upstream (a group's members must not depend on each other).
      * "sequence" — members listed BEFORE this agent already ran, so they are.

    Raises ValueError if `agent_id` is in no step. That case is a mismatch between
    an agent module's own id and the workflow — the synthesis agents call this at
    import with their id (`upstream_of("report")`), so renaming the agent in
    workflow.json without renaming it in the module lands here. Falling off the end
    of the loop would return EVERY agent, including this one, and the agent would
    quietly synthesize from its own previous output. Failing at container start with
    the reason is the better trade.
    """
    seen: list[str] = []
    for step in STEPS:
        ids = step_agents(step)
        if agent_id in ids:
            if "sequence" in step:
                seen.extend(ids[: ids.index(agent_id)])
            return list(reversed(seen))
        seen.extend(ids)
    raise ValueError(
        f"upstream_of({agent_id!r}): no `steps` entry runs that agent. Agents in the "
        f"topology: {', '.join(NODE_IDS) or '(none)'}. If you renamed this agent in "
        f"workflow.json, rename its folder under app/subagents/ and the id it passes "
        f"to upstream_of() to match.")


def _load_tools() -> dict:
    """The `tools` block from app/workflow.json, injected by the IaC as TOOLS_JSON:

        {"kb":        {"type": "kb", "corpora": [...]},
         "websearch": {"type": "websearch", "maxResults": 10},
         "docs":      {"type": "mcp", "call": "...", "arg": "..."}}

    The app reads this to know HOW to call each tool (its argument shape), so
    adding a data source stays a config change. Only the call-shape fields are
    kept — the rest of a tool entry is deploy-time detail (endpoint, policy,
    schema) that the IaC consumes and the app must never send in a request.
    Falls back to the file when the env var is absent (local dev), then to {}.
    """
    raw = os.getenv("TOOLS_JSON", "").strip()
    if raw:
        try:
            return json.loads(raw)
        except ValueError:
            pass
    # Local dev / BFF: fall back to the `tools` block of the workflow already
    # loaded above, keeping only the call-shape fields the app needs.
    # Must match the projection both IaC paths build (terraform/tools.tf
    # `tools_env`, cdk/lib/orchestrator-stack.ts `toolsEnv`). A field missing here
    # is silently dropped on the fallback path, which is how `publishedFrom` /
    # `publishedTo` came to work under one deployment and not the other.
    keep = ("type", "corpora", "maxResults", "includeDomains", "excludeDomains",
            "publishedFrom", "publishedTo", "call", "arg", "args", "rowFields")
    return {
        name: {k: v for k, v in (spec or {}).items() if k in keep}
        for name, spec in (WORKFLOW.get("tools") or {}).items()
    }


TOOLS: dict = _load_tools()
