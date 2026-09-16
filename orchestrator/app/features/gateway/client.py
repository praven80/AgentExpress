"""MCP access through the AgentCore Gateway.

All MCP tools are reached through one authenticated Gateway endpoint. The runtime
fetches a short-lived client-credentials access token from the configured identity
provider and connects to the Gateway's MCP URL over Streamable HTTP.

There is NO simulated fallback: an unreachable or unpublished tool raises
ToolUnavailable, and a Cedar refusal raises ToolDenied (app/common/errors.py). The
framework never substitutes invented evidence for a tool that did not run.

The token request differs per IdP (GATEWAY_AUTH_FLOW, set by Terraform from the
`idp` variable) — see _token_request() below.

A successful call returns (text, "gateway"); anything else raises. The mode is kept
in the return shape because the telemetry rows and the UI record how the tool was
reached.
"""

import asyncio
import base64
import json
import os
import time
import urllib.parse
import urllib.request
from typing import Tuple

from app.common.errors import ToolDenied, ToolUnavailable
from app.common.config import (
    TOOLS,
    GATEWAY_AUDIENCE,
    GATEWAY_AUTH_FLOW,
    GATEWAY_CLIENT_ID,
    GATEWAY_CLIENT_SECRET,
    GATEWAY_TOKEN_URL,
    GATEWAY_URL,
    MCP_TIMEOUT,
)

_token_cache = {"value": "", "exp": 0.0}


def _token_request() -> urllib.request.Request:
    """Build the client-credentials request for the configured IdP.

    The two providers differ in BOTH how the client authenticates and what it
    asks for, which is why this is a branch rather than one shared request:

      cognito — the client authenticates with HTTP Basic (client_id:secret) and
                requests an OAuth2 `scope`. The resulting token carries
                `client_id` + `scope` and NO `aud`.
      auth0   — the client credentials go in the form body and it requests an
                `audience` (the API identifier). The resulting token carries
                `aud` + `azp` and no `client_id`.

    The Gateway's authorizer is configured to match (see terraform/gateway.tf).
    To add another OIDC provider, add a branch here and one in identity.tf.
    """
    if GATEWAY_AUTH_FLOW == "auth0":
        body = urllib.parse.urlencode({
            "grant_type": "client_credentials",
            "client_id": GATEWAY_CLIENT_ID,
            "client_secret": GATEWAY_CLIENT_SECRET,
            "audience": GATEWAY_AUDIENCE,
        }).encode()
        return urllib.request.Request(
            GATEWAY_TOKEN_URL, data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

    # Default: Cognito.
    body = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "scope": GATEWAY_AUDIENCE,
    }).encode()
    credentials = base64.b64encode(
        f"{GATEWAY_CLIENT_ID}:{GATEWAY_CLIENT_SECRET}".encode()).decode()
    return urllib.request.Request(
        GATEWAY_TOKEN_URL, data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {credentials}",
        },
    )


def _gateway_token() -> str:
    """Client-credentials token for the Gateway (cached until expiry)."""
    now = time.time()
    if _token_cache["value"] and _token_cache["exp"] - 60 > now:
        return _token_cache["value"]
    with urllib.request.urlopen(_token_request(), timeout=10) as resp:  # noqa: S310
        tok = json.loads(resp.read())
    _token_cache["value"] = tok["access_token"]
    _token_cache["exp"] = now + float(tok.get("expires_in", 3600))
    return _token_cache["value"]


async def _gateway_tools():
    """Connect to the Gateway MCP endpoint with a client-credentials token and list
    its tools. The token flow is Cognito or Auth0 per GATEWAY_AUTH_FLOW."""
    from langchain_mcp_adapters.client import MultiServerMCPClient

    token = await asyncio.to_thread(_gateway_token)
    client = MultiServerMCPClient(
        {
            "gateway": {
                "url": GATEWAY_URL,
                "transport": "streamable_http",
                "headers": {"Authorization": f"Bearer {token}"},
            }
        }
    )
    return await client.get_tools()


# How much evidence one tool call may contribute, in characters. Generous by default
# (roughly 5k tokens) and tunable per deployment: too small and agents reason from
# truncated sources and report it as a limitation of the SOURCE.
MAX_EVIDENCE_CHARS = int(os.getenv("MAX_EVIDENCE_CHARS", "20000"))
# Per-result cap, so one enormous document cannot crowd out the other results.
MAX_RESULT_CHARS = int(os.getenv("MAX_RESULT_CHARS", "4000"))

# Where a result list hides, by tool. The Gateway does not normalise this: the KB
# Lambda, the Web Search connector and a Lambda target return {"results": [...]},
# while the AWS Knowledge MCP server returns {"content": {"result": [...]}}.
_RESULT_PATHS = (("results",), ("content", "result"), ("content", "results"),
                 ("result",), ("items",), ("documents",))
# The field holding a result's prose, by tool. Web Search and the KB use `text`;
# the AWS Knowledge MCP server uses `context`.
_TEXT_FIELDS = ("text", "context", "content", "snippet", "passage", "body")
_TITLE_FIELDS = ("title", "name", "documentTitle")
_URL_FIELDS = ("url", "uri", "link", "source")
_DATE_FIELDS = ("publishedDate", "published_date", "published", "lastUpdated")


def _unwrap_mcp(result):
    """Peel the MCP content envelope off a tool result.

    A tool result does NOT arrive as the payload the tool returned. MCP wraps it as a
    list of content blocks, with the real payload as a JSON *string* one level down:

        [{"type": "text", "text": "{\\"results\\": [...]}"}]

    Code that looks for a top-level dict with a "results" key therefore matches
    nothing and falls through to a generic stringify — which is how citations end up
    buried in JSON text instead of reaching the model as fields.
    """
    # A JSON string at any level: parse and recurse.
    if isinstance(result, str):
        s = result.strip()
        if s.startswith("{") or s.startswith("["):
            try:
                return _unwrap_mcp(json.loads(s))
            except ValueError:
                return result
        return result

    # The MCP envelope: a list of content blocks. Merge every text block, since a
    # large answer can be split across several.
    if isinstance(result, list):
        texts = [b.get("text") for b in result
                 if isinstance(b, dict) and b.get("type") == "text" and b.get("text")]
        if texts:
            unwrapped = [_unwrap_mcp(t) for t in texts]
            # One block is the common case; several means a genuine multi-part body.
            return unwrapped[0] if len(unwrapped) == 1 else unwrapped
        return result

    return result


def _result_list(data):
    """The list of results inside `data`, wherever this tool chose to put it."""
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return None
    for path in _RESULT_PATHS:
        node = data
        for key in path:
            if not isinstance(node, dict) or key not in node:
                node = None
                break
            node = node[key]
        if isinstance(node, list):
            return node
    return None


def _first(d: dict, fields) -> str:
    for f in fields:
        v = d.get(f)
        if v:
            return str(v).strip()
    return ""


def _extract_chunks(result) -> str:
    """Flatten a tool result into evidence text, RETAINING its citations.

    Every result becomes a numbered block carrying whichever of title / url /
    published date it actually has, so the model can attribute a finding to a real
    source. Fields a tool does not set are simply absent.

    Citations must survive for two reasons:

      1. Web Search's acceptable-use terms REQUIRE that the citations and links
         returned with each result are retained and displayed in anything shown to
         an end user.
      2. Correctness. Hand the model snippet prose with no URLs and any URL that
         appears in its output is one it INVENTED — indistinguishable from a real
         citation to whoever reads the report.

    Tools disagree about shape, so both the result-list location and the prose
    field name are looked up rather than assumed (see _RESULT_PATHS / _TEXT_FIELDS).
    A shape nothing recognises is returned as pretty JSON rather than dropped: the
    model can still read it, and truncating to nothing would hide real evidence.
    """
    data = _unwrap_mcp(result)

    # A multi-part body unwraps to a LIST OF PAYLOADS, each with its own result
    # list. Flatten them, or the outer list is mistaken for the results themselves
    # and every item looks textless.
    if (isinstance(data, list)
            and any(isinstance(d, dict) and _result_list(d) is not None for d in data)):
        results = []
        for part in data:
            results.extend(_result_list(part) or [])
    else:
        results = _result_list(data)

    if results is None:
        # Unrecognised shape. Keep it readable and keep it whole (up to the cap)
        # instead of silently cutting it to a couple of thousand characters.
        text = data if isinstance(data, str) else json.dumps(data, indent=2, default=str)
        return _clip(text, MAX_EVIDENCE_CHARS)

    blocks: list[str] = []
    used = 0
    for i, r in enumerate(results, 1):
        if isinstance(r, str):
            text, head = r.strip(), [f"[{i}]"]
        elif isinstance(r, dict):
            text = _first(r, _TEXT_FIELDS)
            head = [f"[{i}]"]
            title = _first(r, _TITLE_FIELDS)
            url = _first(r, _URL_FIELDS)
            date = _first(r, _DATE_FIELDS)
            if title:
                head.append(f"title: {title}")
            if url:
                head.append(f"url: {url}")
            if date:
                head.append(f"published: {date}")
        else:
            continue
        if not text:
            continue
        block = " | ".join(head) + "\n" + _clip(text, MAX_RESULT_CHARS)
        # Stop at the budget rather than emitting a half block, and say how many
        # results were dropped so the agent can name it as a limitation.
        if used + len(block) > MAX_EVIDENCE_CHARS:
            remaining = len(results) - (i - 1)
            blocks.append(f"[... {remaining} further result(s) omitted: evidence "
                          f"budget of {MAX_EVIDENCE_CHARS} characters reached]")
            break
        blocks.append(block)
        used += len(block)

    return "\n---\n".join(blocks) or "(no matching context)"


def _clip(s: str, limit: int) -> str:
    """Cut to `limit`, on a word boundary where possible, and SAY it was cut.

    The annotation matters: without it an agent sees evidence ending mid-word and
    cannot tell whether the SOURCE was incomplete or the framework trimmed it — and
    it reports the wrong one as a data limitation.
    """
    if len(s) <= limit:
        return s
    cut = s[:limit]
    space = cut.rfind(" ")
    if space > limit * 0.8:
        cut = cut[:space]
    return cut + f"\n[... truncated by the framework at {limit} characters]"


# Error text that means "the Gateway's Cedar policy engine refused this call",
# as opposed to a plain outage — so the UI can show "denied by policy".
_DENIED_MARKERS = ("denied", "forbidden", "not authorized", "unauthorized",
                   "accessdenied", "policy")


def _tool_arguments(tool_key: str, query: str) -> dict:
    """Build the call arguments for a tool, entirely from its config in TOOLS.

    Every MCP server names its parameters differently, so the parameter name is
    CONFIG, not code. A tools entry may set:

      arg   — the parameter the query goes into            (default "query")
      args  — fixed extra arguments sent on every call     (default none)

    So a server whose search tool takes `question` plus a required `repoName` is
    reachable without touching this file:

        "wiki": { "type": "mcp", "endpoint": "...", "call": "ask_question",
                  "arg": "question", "args": { "repoName": "aws/aws-cdk" } }

    Type-specific handling on top of that:
      websearch — the managed connector's documented schema: `query` (clamped to
                  200 chars) plus optional `maxResults` and a `filters` object.
      kb        — the corpus `filter` is added by retrieve(), which knows the
                  agent's corpus.
    """
    spec = TOOLS.get(tool_key) or {}
    kind = str(spec.get("type", "mcp")).lower()
    arg_name = str(spec.get("arg") or "query")
    fixed = dict(spec.get("args") or {})

    if kind == "websearch":
        # The connector's parameter IS `query`, and the limit is documented.
        args: dict = {"query": query[:200]}
        max_results = spec.get("maxResults")
        if max_results:
            args["maxResults"] = int(max_results)
        # Request-level filters (connector v1.2.0+). These are supplied by the
        # CALLER, so they are scoping, not a security boundary: they compose with
        # the target-level lists and cannot override an exclude or widen beyond an
        # include. Configure targetIncludeDomains / targetExcludeDomains for the
        # enforceable, agent-invisible form (see terraform/tools.tf).
        filters: dict = {}
        domain_filter = {
            k: v for k, v in (
                ("include", spec.get("includeDomains") or []),
                ("exclude", spec.get("excludeDomains") or []),
            ) if v
        }
        if domain_filter:
            filters["domainFilter"] = domain_filter
        # Published-date bounds, inclusive, ISO-8601 UTC. Web results only.
        date_filter = {
            k: v for k, v in (
                ("from", spec.get("publishedFrom") or ""),
                ("to", spec.get("publishedTo") or ""),
            ) if v
        }
        if date_filter:
            filters["publishedDateFilter"] = date_filter
        if filters:
            args["filters"] = filters
        args.update(fixed)
        return args

    out = {arg_name: query}
    out.update(fixed)
    return out


def _select_tool(tools, tool_key: str):
    """Pick which published tool to invoke for a workflow.json tool label.

    The Gateway names every tool "<targetName>___<toolName>". A target can publish
    SEVERAL tools (the AWS Documentation MCP server publishes five), so which to call is
    config: set `call` on the tools entry. Without it, and with more than one
    candidate, we refuse rather than guess — picking arbitrarily would silently
    call the wrong tool with the wrong arguments.
    """
    prefix = f"{tool_key.lower()}___"
    candidates = [t for t in tools if t.name.lower().startswith(prefix)]
    if not candidates:  # a target that doesn't prefix its tools
        candidates = [t for t in tools if tool_key.lower() in t.name.lower()]

    wanted = str((TOOLS.get(tool_key) or {}).get("call") or "").lower()
    if wanted:
        exact = next((t for t in candidates if t.name.lower() == f"{prefix}{wanted}"), None)
        return exact or next((t for t in candidates if wanted in t.name.lower()), None)

    if len(candidates) == 1:
        return candidates[0]
    return None


async def _query_tool_impl(tool_key: str, query: str,
                           extra_args: dict | None = None) -> Tuple[str, str]:
    """Call a Gateway tool by its workflow.json label. Returns (text, "gateway").

    Raises rather than returning placeholder text — see app/common/errors.py:
      ToolUnavailable — no Gateway configured, the tool isn't published, the call
                        failed, or the target publishes several tools and the
                        config didn't say which to use.
      ToolDenied      — the Cedar policy engine refused the call.
    """
    if not GATEWAY_URL:
        raise ToolUnavailable(
            f"Agent needs tool '{tool_key}' but no Gateway is configured (GATEWAY_URL is "
            f"empty). Deploy with enable_gateway = true / -c enableGateway=true, or remove "
            f"the `tool` binding from the agents that use it in workflow.json.")

    try:
        tools = await asyncio.wait_for(_gateway_tools(), timeout=MCP_TIMEOUT)
    except Exception as e:  # noqa: BLE001
        raise ToolUnavailable(
            f"Could not list tools on the Gateway while calling '{tool_key}': "
            f"{type(e).__name__}: {e}") from e

    tool = _select_tool(tools, tool_key)
    if tool is None:
        available = ", ".join(sorted(t.name for t in tools)) or "(none)"
        prefix = f"{tool_key.lower()}___"
        matching = [t.name for t in tools if t.name.lower().startswith(prefix)]
        if len(matching) > 1:
            raise ToolUnavailable(
                f"Target '{tool_key}' publishes {len(matching)} tools ({', '.join(matching)}), "
                f"so workflow.json must say which to call: add \"call\": \"<toolName>\" to "
                f"tools.{tool_key}.")
        raise ToolUnavailable(
            f"The Gateway publishes no '{tool_key}' tool, so the agent has no evidence to "
            f"work from. Tools available: {available}. Check that the target reached READY, "
            f"and that `call` in workflow.json matches a published name minus the "
            f"'{tool_key}___' prefix. Note the Gateway composes names as "
            f"'<target>___<tool>' and AWS also prefixes its own managed tools with "
            f"'aws___', so a name may be doubly prefixed.")

    args = _tool_arguments(tool_key, query)
    if extra_args:
        args.update(extra_args)
    try:
        result = await asyncio.wait_for(tool.ainvoke(args), timeout=MCP_TIMEOUT)
    except Exception as e:  # noqa: BLE001
        # A Cedar DENY (ENFORCE mode) surfaces as an authorization error from the
        # Gateway — distinguish it from an outage so the operator knows the policy
        # is working as configured rather than something being broken.
        if any(k in str(e).lower() for k in _DENIED_MARKERS):
            raise ToolDenied(
                f"Tool '{tool.name}' was denied by the Cedar policy engine for this call "
                f"(arguments: {sorted(args)}). Widen the permit in the workflow.json `tools` "
                f"entry if this should be allowed.") from e
        raise ToolUnavailable(
            f"Tool '{tool.name}' call failed: {type(e).__name__}: {e}") from e

    return (_extract_chunks(result), "gateway")


def _kb_tool_key() -> str:
    """The workflow.json label of the tool declared with type="kb"."""
    return next((k for k, v in TOOLS.items() if str(v.get("type", "")).lower() == "kb"), "kb")


# --- observability wrappers -------------------------------------------------
# Public entry points: open an OTEL span, time the call, and record a telemetry
# row (both best-effort), then return exactly what the impl returned. Metering
# never alters behaviour.

def _record_tool(provider: str, query: str, latency_ms: int, result: Tuple[str, str]) -> None:
    try:
        from app.features.observability import meter
        text = result[0] if isinstance(result, tuple) and result else ""
        meter.record_tool(provider=provider, query=query, latency_ms=latency_ms,
                          mode=(result[1] if isinstance(result, tuple) and len(result) > 1 else "gateway"),
                          result_text=text)
    except Exception:  # noqa: BLE001 - metering must never break a call
        pass


def _record_policy(tool: str, result_mode: str, latency_ms: int,
                   filter_value: str | None = None) -> None:
    """Record the Gateway Cedar-policy decision for a tool call. Only when a real
    Gateway (and thus a policy engine) is in play."""
    pmode = os.getenv("GATEWAY_POLICY_MODE", "")
    if not pmode:
        return  # no policy engine attached
    if result_mode == "denied":
        decision = "denied"
    elif result_mode == "gateway":
        decision = "log-only" if pmode.upper() == "LOG_ONLY" else "allowed"
    else:
        return  # gateway not used -> no policy evaluation happened
    try:
        from app.features.observability import meter
        meter.record_policy(tool=tool, decision=decision,
                            filter_value=filter_value or "", mode=pmode,
                            latency_ms=latency_ms)
    except Exception:  # noqa: BLE001
        pass


async def query_tool(tool_key: str, query: str) -> Tuple[str, str]:
    """Call the tool an agent is bound to (its `tool` label in workflow.json)."""
    from app.features.observability import otel
    kind = str((TOOLS.get(tool_key) or {}).get("type", "mcp")).lower()
    start = time.perf_counter()
    with otel.span(f"tool.{tool_key}",
                   **{"gen_ai.tool.name": tool_key, "gen_ai.operation.name": kind}) as sp:
        result = await _query_tool_impl(tool_key, query)
        if sp is not None:
            try:
                sp.set_attribute("gen_ai.tool.mode", result[1])
            except Exception:  # noqa: BLE001
                pass
    latency_ms = int((time.perf_counter() - start) * 1000)
    _record_tool(tool_key, query, latency_ms, result)
    # Cedar action id for a target-level permit is the target name itself; for a
    # tool-specific permit it is "<target>___<tool>". Report the target, which is
    # what the generated policy is keyed on.
    _record_policy(tool_key, result[1], latency_ms)
    return result


async def retrieve(query: str, doc_type: str | None = None) -> Tuple[str, str]:
    """Retrieve grounded context from the Knowledge Base tool via the Gateway.

    `doc_type` scopes retrieval to one corpus (a top-level folder under kb_docs/).
    The generated Cedar permit restricts which corpora are allowed, so a call with
    a corpus outside the declared list is denied server-side.
    """
    from app.features.observability import otel
    tool_key = _kb_tool_key()
    extra = {"filter": doc_type} if doc_type else None
    start = time.perf_counter()
    with otel.span(f"tool.{tool_key}.retrieve",
                   **{"gen_ai.tool.name": tool_key, "gen_ai.operation.name": "retrieve",
                      "kb.doc_type": doc_type}) as sp:
        result = await _query_tool_impl(tool_key, query, extra_args=extra)
        if sp is not None:
            try:
                sp.set_attribute("gen_ai.tool.mode", result[1])
            except Exception:  # noqa: BLE001
                pass
    latency_ms = int((time.perf_counter() - start) * 1000)
    _record_tool(tool_key, query, latency_ms, result)
    _record_policy(f"{tool_key}___retrieve", result[1], latency_ms, filter_value=doc_type)
    return result
