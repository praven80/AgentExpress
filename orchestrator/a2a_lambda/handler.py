"""A real A2A server, so the `runtime: "a2a"` placement ships with something live.

Every other capability in this sample has a working agent proving it: `kb` has one,
`websearch` has one, `mcp` has one, `lambda` has one. `a2a` had documentation and a
test suite, and nothing you could watch run — because that placement needs an agent
THIS DEPLOYMENT DOES NOT OWN, and a committed placeholder URL would have failed every
run. So the framework deploys a stand-in: a genuine A2A server, in its own Lambda,
behind its own Function URL, reached over the protocol exactly as a third party's
agent would be.

It is a STAND-IN, not a mock. It speaks real JSON-RPC 2.0, publishes a real Agent
Card, and answers by really calling Bedrock. The only fiction is the org chart: it
happens to live in your account. Cross that out and nothing about the client changes
— which is the property worth demonstrating.

    "compliance_review": {
      "name": "Compliance Review",
      "runtime": "a2a",
      "source": "a2a_lambda",     <- the framework deploys me and injects my URL
      "skill": "compliance",
      "produces": "compliance-review"
    }

Point an agent at a real partner instead and this function stops being deployed:
`"agentCard": "https://theirs.example/agent"` and `source` goes away, the same way a
`tools` entry swaps `source` for `lambdaArn`.

THE THREE ENDPOINTS, per the A2A spec and the AgentCore A2A contract
  GET  /.well-known/agent-card.json  discovery: identity, skills, where to POST
  POST /                             JSON-RPC 2.0: `message/send`, `tasks/get`
  GET  /ping                         health

WHY IT ANSWERS SYNCHRONOUSLY. A2A lets a server return a Task that is still `working`
and be polled. This one finishes inside the request because a Lambda has nowhere to
keep a task between invocations — there is no store here, and inventing one would be
inventing infrastructure the demonstration does not need. The client handles both, and
`tests/test_a2a.py` covers the polling path; a real partner with a queue behind it will
exercise it.

SECURITY. The Function URL is `AWS_IAM`, so this endpoint is not public: a caller must
present a SigV4 signature from a principal allowed to invoke it, which is the
orchestrator's execution role and nothing else. That is why the client has an
`auth: "sigv4"` mode — no bearer token exists to leak, nothing to rotate, and no
secret in `workflow.json`. A Function URL with `authType: NONE` guarded by a shared
token would have been less code and materially worse.
"""

from __future__ import annotations

import json
import os
import re

import boto3

MODEL_ID = os.getenv("BEDROCK_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
REGION = os.getenv("AWS_REGION", "us-east-1")
AGENT_NAME = os.getenv("A2A_AGENT_NAME", "Partner Agent")
MAX_TOKENS = int(os.getenv("A2A_MAX_TOKENS", "2000"))

CARD_PATH = "/.well-known/agent-card.json"
PROTOCOL_VERSION = "0.3"

# JSON-RPC error codes. The two standard ones plus the A2A code for an unknown task,
# so a client that switches on them sees what it expects.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
TASK_NOT_FOUND = -32051

_bedrock = None


def _client():
    global _bedrock
    if _bedrock is None:
        _bedrock = boto3.client("bedrock-runtime", region_name=REGION)
    return _bedrock


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------
#
# Two, so the sample can show two remote agents that are genuinely different rather
# than one function called twice. Each is a role plus a shape — the same thing an
# agent under app/subagents/ carries in its prompts.py, which is the point: from the
# orchestrator's side there is no way to tell that this one lives somewhere else.
#
# Deliberately NOT this framework's asset contract. A remote agent has its own output
# shape, and demanding ours is exactly what makes a third-party agent un-integrable.
# The orchestrator takes the text and the downstream synthesis agents cite it like any
# other input.

SKILLS: dict[str, dict] = {
    "compliance": {
        "id": "compliance",
        "name": "Compliance review",
        "description": "Reviews a proposed design against common regulatory and data-handling "
                       "obligations, and names the controls that would have to exist.",
        "tags": ["compliance", "governance", "risk"],
        "prompt": (
            "You are an independent compliance reviewer at a firm the requester does not "
            "control. Review the request and the evidence supplied for regulatory and "
            "data-handling exposure.\n\n"
            "RULES\n"
            "1. Name concrete obligations that plausibly apply (data residency, retention, "
            "audit trail, access control, personal-data handling) and say WHY each applies to "
            "THIS design. Do not list a compliance regime you cannot tie to something in the "
            "inputs.\n"
            "2. For each obligation, name the control that would satisfy it.\n"
            "3. Separate what the inputs ESTABLISH from what you are INFERRING. You are an "
            "outside reviewer; say when you are assuming.\n"
            "4. If the inputs are too thin to review, say so plainly and say what you would "
            "need. Do not pad.\n"
            "5. No preamble about being an AI or about the request's quality."
        ),
        "shape": ('{"reviewer": "compliance", "verdict": "one sentence", '
                  '"obligations": [{"obligation": "...", "whyItApplies": "...", '
                  '"control": "...", "basis": "established|inferred"}], '
                  '"assumptions": ["..."], "notReviewable": ["..."]}'),
    },
    "resilience": {
        "id": "resilience",
        "name": "Resilience review",
        "description": "Stress-tests a proposed design for failure modes, blast radius and "
                       "recovery, and names what is missing.",
        "tags": ["resilience", "reliability", "operations"],
        "prompt": (
            "You are an independent resilience reviewer at a firm the requester does not "
            "control. Stress-test the proposed design.\n\n"
            "RULES\n"
            "1. Name failure modes that follow from what the inputs actually describe — a "
            "dependency that can be slow or absent, a step with no retry, state that cannot be "
            "rebuilt. Tie each to something in the inputs.\n"
            "2. For each, give the blast radius and the recovery path.\n"
            "3. Name what the design does NOT say that you would need to judge it.\n"
            "4. Separate what the inputs establish from what you are inferring.\n"
            "5. If the inputs are too thin, say so plainly. Do not pad.\n"
            "6. No preamble about being an AI or about the request's quality."
        ),
        "shape": ('{"reviewer": "resilience", "verdict": "one sentence", '
                  '"failureModes": [{"mode": "...", "blastRadius": "...", "recovery": "...", '
                  '"basis": "established|inferred"}], '
                  '"unanswered": ["..."], "notReviewable": ["..."]}'),
    },
}

DEFAULT_SKILL = "compliance"


def _skill(name: str) -> dict:
    return SKILLS.get(str(name or "").strip().lower()) or SKILLS[DEFAULT_SKILL]


# ---------------------------------------------------------------------------
# The Agent Card
# ---------------------------------------------------------------------------

def agent_card(base_url: str, skill_name: str) -> dict:
    """Discovery document. `url` is absolute and points back at this Function URL,
    because a client is entitled to POST wherever the card says — including a
    different host from the card's own origin."""
    skill = _skill(skill_name)
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "name": AGENT_NAME,
        "description": skill["description"],
        "version": "1.0.0",
        "url": base_url,
        "preferredTransport": "JSONRPC",
        "supportedInterfaces": [{"transport": "JSONRPC", "url": base_url}],
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["application/json"],
        "capabilities": {"streaming": False, "pushNotifications": False},
        "skills": [{k: skill[k] for k in ("id", "name", "description", "tags")}],
    }


# ---------------------------------------------------------------------------
# The work
# ---------------------------------------------------------------------------

def _task_text(params: dict) -> str:
    """The text the client sent, out of `params.message.parts`.

    Mirrors the client's own tolerance: `kind`, the legacy `type`, or neither. A server
    that accepts only the current form rejects clients that are merely older.
    """
    message = params.get("message") if isinstance(params, dict) else None
    parts = (message or {}).get("parts") if isinstance(message, dict) else None
    out: list[str] = []
    for part in parts if isinstance(parts, list) else []:
        if not isinstance(part, dict):
            continue
        kind = str(part.get("kind") or part.get("type") or "").lower()
        if kind in ("", "text") and isinstance(part.get("text"), str):
            out.append(part["text"])
        elif kind == "data" and part.get("data") is not None:
            out.append(json.dumps(part["data"], ensure_ascii=False))
    return "\n".join(out).strip()


def review(task_text: str, skill_name: str) -> str:
    """Do the actual work: one Bedrock call, in the reviewer's voice.

    Raises on failure rather than returning a placeholder — the same standard the rest
    of this sample holds. A stand-in that fabricates when the model is unavailable
    would teach exactly the wrong lesson about remote agents.
    """
    skill = _skill(skill_name)
    response = _client().converse(
        modelId=MODEL_ID,
        system=[{"text": f"{skill['prompt']}\n\nReturn ONLY valid JSON of this shape:\n"
                         f"{skill['shape']}"}],
        messages=[{"role": "user", "content": [{"text": task_text or "(no inputs supplied)"}]}],
        inferenceConfig={"maxTokens": MAX_TOKENS, "temperature": 0},
    )
    blocks = response.get("output", {}).get("message", {}).get("content", [])
    text = "\n".join(b.get("text", "") for b in blocks if isinstance(b, dict)).strip()
    if not text:
        raise RuntimeError("the model returned no content")
    # Unwrap a fenced block if the model added one, so the client gets JSON and not
    # markdown. Not a parse — just the fence.
    fenced = re.match(r"^```(?:json)?\s*\n(.*)\n```\s*$", text, re.DOTALL)
    return fenced.group(1).strip() if fenced else text


# ---------------------------------------------------------------------------
# JSON-RPC
# ---------------------------------------------------------------------------

def _ok(rpc_id, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


def _err(rpc_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}}


def _completed_task(task_id: str, context_id: str, text: str) -> dict:
    """A finished Task carrying its answer as an artifact.

    An artifact rather than a status message on purpose: the artifact is the
    deliverable, the status message is narration, and the client prefers the former.
    """
    return {
        "id": task_id,
        "contextId": context_id,
        "status": {"state": "completed"},
        "artifacts": [{
            "artifactId": f"{task_id}-review",
            "name": "review",
            "parts": [{"kind": "text", "text": text}],
        }],
    }


def handle_rpc(body: dict, skill_name: str) -> dict:
    rpc_id = body.get("id")
    method = str(body.get("method") or "")
    params = body.get("params") if isinstance(body.get("params"), dict) else {}

    if body.get("jsonrpc") != "2.0":
        return _err(rpc_id, INVALID_REQUEST, 'Invalid request - "jsonrpc" must be "2.0"')

    if method == "message/send":
        task_text = _task_text(params)
        context_id = str(params.get("contextId") or "")
        task_id = str((params.get("message") or {}).get("messageId") or "task")
        try:
            text = review(task_text, skill_name)
        except Exception as e:  # noqa: BLE001 - any failure is reported as a failed task
            # A failed Task, not a JSON-RPC error: the request was valid, the work did
            # not succeed. The client distinguishes these, and only this one carries a
            # reason the reviewer can read.
            return _ok(rpc_id, {
                "id": task_id, "contextId": context_id,
                "status": {"state": "failed", "message": {"parts": [
                    {"kind": "text", "text": f"{type(e).__name__}: {e}"}]}},
            })
        return _ok(rpc_id, _completed_task(task_id, context_id, text))

    if method == "tasks/get":
        # Answering synchronously means a task is finished before the client could ask
        # about it, and there is no store to look one up in. Say that, rather than
        # inventing a `working` state that would be polled forever.
        return _err(rpc_id, TASK_NOT_FOUND,
                    "Task not found - this agent completes synchronously and keeps no task store")

    return _err(rpc_id, METHOD_NOT_FOUND,
                f'Method not found - "{method}". This agent implements message/send.')


# ---------------------------------------------------------------------------
# Function URL entry point
# ---------------------------------------------------------------------------

def _response(status: int, payload: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", "A2A-Version": PROTOCOL_VERSION},
        "body": json.dumps(payload, ensure_ascii=False),
    }


def _origin(event: dict) -> str:
    """This function's own https origin, from the request.

    Taken from the Host header rather than configured, so the card advertises the URL
    the client actually reached — which makes it correct under an alias, and avoids a
    second place to keep the URL in sync.
    """
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    host = headers.get("host") or ""
    return f"https://{host}" if host else ""


def _skill_from_path(path: str) -> str:
    """The skill is the FIRST PATH SEGMENT — `/compliance`, `/resilience`.

    A path and not a query string, because a client appends the well-known card path to
    whatever base URL it was given. `https://host/?skill=x` + `/.well-known/...` puts
    the path after the query and resolves to nothing; `https://host/compliance` +
    `/.well-known/...` is a real URL. Found by reading back the card this function
    generated.
    """
    for segment in str(path or "").split("/"):
        if segment and segment.lower() in SKILLS:
            return segment.lower()
    return DEFAULT_SKILL


def lambda_handler(event, _context=None):
    """Route on method + path, the way a Function URL delivers them.

    The skill comes from the path, so ONE deployed function can back several
    workflow.json agents that are genuinely different reviewers — `/compliance` and
    `/resilience`. The alternative was a Lambda per skill: more infrastructure to
    demonstrate the same protocol.
    """
    http = (event.get("requestContext") or {}).get("http") or {}
    method = str(http.get("method") or event.get("httpMethod") or "GET").upper()
    path = str(event.get("rawPath") or http.get("path") or "/")
    skill_name = _skill_from_path(path)

    if method == "GET" and path.rstrip("/").endswith(CARD_PATH.rstrip("/")):
        return _response(200, agent_card(f"{_origin(event)}/{skill_name}", skill_name))

    if method == "GET" and path.rstrip("/").endswith("/ping"):
        return _response(200, {"status": "Healthy"})

    if method != "POST":
        return _response(404, {"error": f"Not found - {method} {path}"})

    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        import base64
        raw = base64.b64decode(raw).decode()
    try:
        body = json.loads(raw)
    except ValueError:
        return _response(200, _err(None, PARSE_ERROR, "Parse error - body is not valid JSON"))
    if not isinstance(body, dict):
        return _response(200, _err(None, INVALID_REQUEST, "Invalid request - expected an object"))

    result = handle_rpc(body, skill_name)
    # HTTP 200 with the error in the body, which is what the A2A spec prescribes. (The
    # client also copes with servers that return the real status code, because
    # AgentCore Runtime does.)
    return _response(200, result)
