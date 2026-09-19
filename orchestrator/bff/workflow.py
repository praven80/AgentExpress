"""The BFF's view of workflow.json: where it comes from, and what the browser sees.

TWO JOBS, AND THE SECOND IS A TRUST BOUNDARY.

`RAW` is the workflow exactly as the customer wrote it. `VIEW` is the projection
served to the browser by `GET /api/workflow`, and it is deliberately a SUBSET:
`tools.*.endpoint`, `toolSchema`, `policy`, credentials and `guardrail.deniedWords`
are deploy-time detail that the page has no use for and no business holding. So the
projection is an allow-list of what the UI reads, not a blocklist of what to hide —
a new secret-ish key added to workflow.json cannot leak by omission.

WHY THIS IS PYTHON AND NOT IaC
------------------------------
This projection used to be built TWICE, by `local.bff_workflow` in
terraform/bff.tf and `buildBffWorkflow()` in cdk/lib/orchestrator-stack.ts, and
shipped to the Lambda in a `WORKFLOW_JSON` environment variable. Both of those were
problems.

Two implementations drifted, and the drift was silent: the CDK copy once emitted
`mcp`/`rag` after workflow.json had renamed them to `tool`/`corpus`, and omitted
`evalAgents` and `chatbot` entirely — so CDK deployments quietly lost the
data-source chips, the Evaluate buttons and the in-app assistant while Terraform
ones kept them. A parity test was added to catch that class of bug. One
implementation cannot have that class of bug.

The environment variable was worse, because it was a CEILING. Lambda caps the whole
environment at 4 KB and that quota cannot be raised
(https://docs.aws.amazon.com/lambda/latest/dg/configuration-envvars.html). Both IaC
paths therefore carried a 3400-byte guard on the projection, and the shipped
ten-agent workflow measured 3153 bytes — roughly 147 bytes per agent, so ELEVEN
agents fit and twelve did not. A customer with fifteen agents got a deploy failure
whose only advice was to shorten their agent names. That is not a framework that
works for every use case.

Reading the file from the deployment package removes the ceiling outright rather
than raising it. SSM Parameter Store was considered and rejected: its standard tier
is the same 4096 bytes and its advanced tier is 8 KB and billed
(https://docs.aws.amazon.com/systems-manager/latest/userguide/parameter-store-advanced-parameters.html),
so it swaps an 11-agent ceiling for a 22-agent one. S3 was considered and rejected
too: it needs a private bucket, a bucket policy, an IAM grant and a cold-start
network call on both IaC paths, where the zip needs none of those. The Lambda
package limit is 250 MB unzipped, which is not a ceiling anyone will reach with a
JSON config file.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

# workflow.json as shipped INSIDE this Lambda's deployment package. Both IaC paths
# put it here (terraform/bff.tf's archive_file, and the staged asset directory the
# CDK stack builds), so the BFF and the orchestrator runtime read the same bytes
# from the same file the customer edits.
_BUNDLED = Path(__file__).resolve().parent / "workflow.json"

# The same file in a SOURCE CHECKOUT, where nothing has staged a copy next to this
# module yet. Only ever hit outside a deployment — in the Lambda the bundled copy is
# always there — and it exists so this module is importable from a clone, which is
# what tests and local dev do.
_IN_TREE = Path(__file__).resolve().parent.parent / "app" / "workflow.json"

#: An agent with no tool and no recognisable `access` hint. An em dash, because a
#: blank chip reads like a rendering failure.
NO_SOURCE = "\u2014"

#: `access[0]` phrasing -> the chip shown for an agent with no tool. Matched on a
#: generic English word rather than an exact string, so a customer's own wording
#: ("session input", "the upstream assets") still produces a chip.
_ACCESS_HINTS = ((re.compile("session", re.IGNORECASE), "Session input"),
                 (re.compile("upstream", re.IGNORECASE), "Upstream agent outputs"))


def load_raw() -> dict:
    """The workflow: the env var, else the bundled copy, else the one in the tree.

    Same three-step precedence `app/common/config.py` uses for the runtime, so there
    is one convention rather than two. In a deployed Lambda only the bundled copy
    exists; in a checkout only the in-tree one does; the env var is the override
    tests use.
    """
    inline = os.getenv("WORKFLOW_JSON")
    if inline:
        return json.loads(inline)
    for candidate in (_BUNDLED, _IN_TREE):
        if candidate.exists():
            return json.loads(candidate.read_text())
    # Not a silent empty default. Every route derives its agent list from this, so an
    # empty workflow renders a UI with no nodes and no error at all — which is
    # indistinguishable from a broken deployment. The old default was exactly that.
    raise RuntimeError(
        f"the BFF found no workflow: neither {_BUNDLED} (the deployment package) nor "
        f"{_IN_TREE} (a source checkout) exists, and WORKFLOW_JSON is unset. Both IaC "
        f"paths are supposed to bundle app/workflow.json next to handler.py — see the "
        f"archive_file in terraform/bff.tf and stageBffPackage in "
        f"cdk/lib/orchestrator-stack.ts.")


def a2a_host(card: str) -> str:
    """The host of an Agent Card URL, for the chip on a remote agent.

    Host only, never the path: the chip is narrow and a full URL is mostly noise.
    The scheme match is case-insensitive but the host is NOT lower-cased — an
    earlier HCL version used a case-sensitive replace and rendered "HTTPS://B.example"
    as the host "HTTPS:".
    """
    return re.sub(r"(?i)^https?://", "", str(card or "")).split("/")[0] or "remote"


def data_source(agent: dict, tool_types: dict[str, str]) -> str:
    """The UI's data-source chip for one agent.

    Derived from the tool's declared TYPE rather than its name, so a customer's new
    tool gets a sensible chip with no UI change.
    """
    # A remote agent is matched FIRST because it has no `tool` by construction — it
    # reaches its own data sources — and it is the one agent on the diagram whose
    # provenance a reviewer most needs to see. Without this it rendered as an em dash.
    if str(agent.get("runtime") or "main") == "a2a":
        # A framework-deployed stand-in is labelled by its `source`, because its URL
        # is a deploy-time value and there is no host to show. Labelling it "remote"
        # said nothing.
        origin = agent.get("source") or a2a_host(agent.get("agentCard", ""))
        return f"A2A \u00b7 {origin}"

    tool = agent.get("tool")
    if tool:
        kind = tool_types.get(tool)
        if kind == "kb":
            return f"Knowledge Base \u00b7 {agent.get('corpus') or 'all'}"
        if kind == "websearch":
            return "Web Search"
        if kind == "openapi":
            return f"REST API \u00b7 {tool}"
        if kind == "lambda":
            return f"Function \u00b7 {tool}"
        return f"MCP \u00b7 {tool}"

    access = (agent.get("access") or [""])[0] or ""
    for pattern, label in _ACCESS_HINTS:
        if pattern.search(access):
            return label
    return NO_SOURCE


def _ui(workflow: dict) -> dict | None:
    """Presentation strings, so re-branding is a workflow.json edit.

    `*Note` keys and empty values are dropped: they are notes to whoever edits the
    config, and an empty string would override the page's own fallback with nothing.
    """
    block = workflow.get("ui")
    if not block:
        return None
    return {k: v for k, v in block.items()
            if not k.endswith("Note") and v is not None and v != ""}


def project(workflow: dict) -> dict:
    """Trim `workflow` to what the BFF and the UI actually read.

    An allow-list, key by key. Adding a key to workflow.json does NOT make it
    visible to the browser until it is named here, which is what makes the trust
    boundary in the module docstring hold by construction.
    """
    tool_types = {name: str((spec or {}).get("type") or "mcp").lower()
                  for name, spec in (workflow.get("tools") or {}).items()}

    agents = {}
    for agent_id, agent in (workflow.get("agents") or {}).items():
        agents[agent_id] = {
            "name": agent.get("name"),
            "kind": agent.get("kind") or "sync",
            "runtime": agent.get("runtime") or "main",
            "tool": agent.get("tool"),
            "corpus": agent.get("corpus"),
            "model": agent.get("model"),
            "source": data_source(agent, tool_types),
        }

    chatbot_cfg = ((workflow.get("orchestrator") or {}).get("chatbot")) or {}
    chatbot: dict | None = None
    if chatbot_cfg.get("enabled") is not None:
        chatbot = {
            "enabled": chatbot_cfg.get("enabled"),
            "model": chatbot_cfg.get("model"),
            "greeting": chatbot_cfg.get("greeting"),
            "placeholder": chatbot_cfg.get("placeholder"),
            # Only the DISABLED flags. `chatbot._enabled_tools` defaults anything it
            # is not told about to on, so the two are equivalent — and this keeps the
            # payload the page downloads proportional to what was switched off.
            "tools": {k: v for k, v in (chatbot_cfg.get("tools") or {}).items()
                      if v is False},
        }

    # RBAC rules, for the UI to disable controls it knows will 403. Shipped only
    # when something is actually restricted: with no `actions` map authz.py is a
    # no-op and there is nothing for the page to act on.
    authorization_cfg = workflow.get("authorization") or {}
    actions = authorization_cfg.get("actions") or {}
    authorization = ({"groupsClaim": authorization_cfg.get("groupsClaim"),
                      "actions": actions} if actions else None)

    return {
        "agents": agents,
        "steps": workflow.get("steps") or [],
        # Which agents show an Evaluate button.
        "evalAgents": [
            agent_id for agent_id, agent in (workflow.get("agents") or {}).items()
            if (((agent.get("agentcore") or {}).get("evaluations") or {})
                .get("enabled") is True)],
        "chatbot": chatbot,
        "ui": _ui(workflow),
        "authorization": authorization,
    }


#: The customer's workflow, unmodified — for anything server-side that needs the
#: full picture (`authz.py` reads `authorization` from here). Resolved at import,
#: like the runtime's own config, because a Lambda serves one workflow for its whole
#: life. Tests re-import the module to change it.
RAW: dict = load_raw()

#: The only thing that may be sent to a browser. See the module docstring.
VIEW: dict = project(RAW)

#: Agent ids in declaration order — the per-node skeleton a new run starts from.
NODE_IDS: list[str] = list(RAW.get("agents") or {})

#: The topic a run starts with when the caller sends none. From workflow.json, never
#: a literal: this sample's topic has nothing to do with a customer's use case.
DEFAULT_TOPIC: str = str((RAW.get("ui") or {}).get("defaultTopic") or "")
