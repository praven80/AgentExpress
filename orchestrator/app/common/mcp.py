"""MCP access through the AgentCore Gateway.

All MCP tools are reached through one Cognito-authed Gateway endpoint. The runtime
fetches a short-lived Cognito (client-credentials) access token and connects to the
Gateway's MCP URL over Streamable HTTP. When GATEWAY_URL is unset (e.g. local dev
without the Gateway), calls fall back to a simulated response.

Every call returns (text, mode) where mode is "gateway" or "simulated", so the
UI/timeline can show how the tool was reached.
"""

import asyncio
import json
import time
import urllib.parse
import urllib.request
from typing import Tuple

from app.common.config import (
    GATEWAY_AUDIENCE,
    GATEWAY_CLIENT_ID,
    GATEWAY_CLIENT_SECRET,
    GATEWAY_TOKEN_URL,
    GATEWAY_URL,
    MCP_TIMEOUT,
)

_token_cache = {"value": "", "exp": 0.0}


def _gateway_token() -> str:
    """Cognito client-credentials token for the Gateway (cached until expiry)."""
    now = time.time()
    if _token_cache["value"] and _token_cache["exp"] - 60 > now:
        return _token_cache["value"]
    body = urllib.parse.urlencode(
        {
            "grant_type": "client_credentials",
            "scope": GATEWAY_AUDIENCE,
        }
    ).encode()
    # Cognito client-credentials requires HTTP Basic auth (client_id:client_secret).
    import base64
    credentials = base64.b64encode(f"{GATEWAY_CLIENT_ID}:{GATEWAY_CLIENT_SECRET}".encode()).decode()
    req = urllib.request.Request(
        GATEWAY_TOKEN_URL,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {credentials}",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
        tok = json.loads(resp.read())
    _token_cache["value"] = tok["access_token"]
    _token_cache["exp"] = now + float(tok.get("expires_in", 3600))
    return _token_cache["value"]


async def _gateway_tools():
    """Connect to the Gateway MCP endpoint with a Cognito token and list tools."""
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


def _extract_chunks(result) -> str:
    """Pull chunk text out of the KB retrieve tool result (JSON string or dict)."""
    data = result
    if isinstance(result, str):
        try:
            data = json.loads(result)
        except Exception:  # noqa: BLE001
            return result
    if isinstance(data, dict) and "results" in data:
        texts = [r.get("text", "") for r in data.get("results", [])]
        return "\n---\n".join(t for t in texts if t) or "(no matching context)"
    return json.dumps(data)[:2000]


async def _query_mcp_impl(server_key: str, query: str) -> Tuple[str, str]:
    """Call a provider tool on the Gateway (e.g. the "knowledge" MCP target). Returns (text, mode).

    When the Gateway is reachable but no tool matches the provider label, the
    provider simply isn't wired for this POC — report that plainly as a simulated
    (unavailable) response rather than implying data came back.
    """
    if not GATEWAY_URL:
        return (f"[simulated {server_key}] provider not live; no data returned for '{query}'.",
                "simulated")
    try:
        tools = await asyncio.wait_for(_gateway_tools(), timeout=MCP_TIMEOUT)
        tool = next((t for t in tools if server_key.lower() in t.name.lower()), None)
        if tool is None:
            return (f"[simulated {server_key}] Gateway is reachable, but no '{server_key}' "
                    f"provider tool is registered (provider not enabled for this POC); "
                    f"no {server_key} data returned.", "simulated")
        result = await asyncio.wait_for(tool.ainvoke({"query": query}), timeout=MCP_TIMEOUT)
        return (_extract_chunks(result), "gateway")
    except Exception as e:  # noqa: BLE001
        return (f"[simulated {server_key}] Gateway unavailable ({type(e).__name__}); "
                f"no {server_key} data returned for '{query}'.", "simulated")


async def _retrieve_impl(query: str, tool_hint: str = "retrieve",
                   doc_type: str | None = None) -> Tuple[str, str]:
    """Retrieve grounded context from the KB tool via the Gateway. Returns (text, mode).

    When `doc_type` is set, the retrieval is scoped to that corpus (the Gateway
    tool applies an `equals` filter on the `doc_type` metadata attribute), so a
    research agent only sees chunks from its own document set.
    """
    if not GATEWAY_URL:
        scope = f" (doc_type={doc_type})" if doc_type else ""
        return (f"[simulated kb] retrieved context for '{query}'{scope}.", "simulated")
    try:
        tools = await asyncio.wait_for(_gateway_tools(), timeout=MCP_TIMEOUT)
        tool = next((t for t in tools if tool_hint in t.name), None)
        if tool is None:
            return (f"[simulated kb] retrieve tool not found for '{query}'.", "simulated")
        args = {"query": query}
        if doc_type:
            args["filter"] = doc_type
        result = await asyncio.wait_for(tool.ainvoke(args), timeout=MCP_TIMEOUT)
        return (_extract_chunks(result), "gateway")
    except Exception as e:  # noqa: BLE001
        return (f"[simulated kb] retrieve unavailable ({type(e).__name__}) for '{query}'.",
                "simulated")


# --- observability wrappers -------------------------------------------------
# Public entry points: time the call and record a telemetry row (best-effort),
# then return exactly what the impl returned. Metering never alters behaviour.

def _record_tool(provider: str, query: str, latency_ms: int, result: Tuple[str, str]) -> None:
    try:
        from app.observability import meter
        meter.record_tool(provider=provider, query=query, latency_ms=latency_ms,
                          mode=(result[1] if isinstance(result, tuple) and len(result) > 1 else "gateway"))
    except Exception:  # noqa: BLE001 - metering must never break a call
        pass


async def query_mcp(server_key: str, query: str) -> Tuple[str, str]:
    start = time.perf_counter()
    result = await _query_mcp_impl(server_key, query)
    _record_tool(server_key, query, int((time.perf_counter() - start) * 1000), result)
    return result


async def retrieve(query: str, tool_hint: str = "retrieve",
                   doc_type: str | None = None) -> Tuple[str, str]:
    start = time.perf_counter()
    result = await _retrieve_impl(query, tool_hint=tool_hint, doc_type=doc_type)
    _record_tool("kb", query, int((time.perf_counter() - start) * 1000), result)
    return result
