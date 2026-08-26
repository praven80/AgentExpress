"""Shared runner for the downstream synthesis sub-agents.

The downstream agents (analysis, recommendation, report) all follow one flow:

  1. gather the APPROVED upstream assets this agent consumes (from graph state),
  2. ask the model for a structured payload that traces claims to those assets
     and separates evidence from interpretation,
  3. the agent maps that payload onto its own validated contract asset.

This is framework plumbing (like research.synthesize); each agent keeps only its
identity — its prompt, its contract, and which upstream assets it reads.
"""

from __future__ import annotations

import json
import re
from typing import Any

from app.common import clock
from app.common.contracts.base import AssetStatus, Source, SourceType

_SOURCE_TYPES = set(SourceType.__args__)


def slug(text: str, limit: int = 48) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:limit] or "request"


def extract_json(text: str) -> dict | None:
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    start = t.find("{")
    if start == -1:
        return None
    t = t[start:]
    # Fast path: the whole object parses.
    end = t.rfind("}")
    if end != -1:
        try:
            return json.loads(t[: end + 1])
        except (ValueError, TypeError):
            pass
    # Resilient path: the model output was truncated mid-JSON (hit the token
    # budget). Rebalance the open braces/brackets and retry, dropping the last
    # (partial) line if needed, so we salvage a usable partial object instead of
    # degrading the whole agent.
    return _repair_json(t)


def _balance_close(s: str) -> str | None:
    """Close any unclosed strings/brackets in a JSON prefix and return the parsed
    object, or None if it still won't parse."""
    stack: list[str] = []
    in_str = False
    esc = False
    for ch in s:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append(ch)
        elif ch == "}" and stack and stack[-1] == "{":
            stack.pop()
        elif ch == "]" and stack and stack[-1] == "[":
            stack.pop()
    fixed = s
    if in_str:
        fixed += '"'
    fixed = re.sub(r",\s*$", "", fixed.rstrip())
    fixed += "".join("}" if o == "{" else "]" for o in reversed(stack))
    try:
        return json.loads(fixed)
    except (ValueError, TypeError):
        return None


def _repair_json(t: str) -> dict | None:
    lines = t.splitlines()
    # Try the full prefix first, then progressively drop trailing (truncated)
    # lines until a balanced close parses.
    for cut in range(len(lines), 0, -1):
        candidate = "\n".join(lines[:cut])
        obj = _balance_close(candidate)
        if isinstance(obj, dict):
            return obj
    return None


def _parse(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {}


def brief(ctx) -> tuple[dict, str]:
    """The approved request-brief plus its display title."""
    b = _parse(ctx.input("intake"))
    title = b.get("title") or ctx.topic or ctx.agent_id
    return b, str(title)


def prior_version(ctx) -> int:
    """This agent's own previous output version (for the revise cycle)."""
    raw = ctx.input(ctx.agent_id)
    if not raw:
        return 1
    try:
        return int(json.loads(raw).get("version", 1)) + 1
    except (TypeError, ValueError):
        return 1


def envelope(ctx, meta: dict, asset_type_slug: str) -> dict[str, Any]:
    """AssetEnvelope kwargs (by alias) shared by every synthesized asset."""
    return {
        "assetId": f"asset-{asset_type_slug}-{slug(meta['title'])}-v{meta['version']}",
        "version": meta["version"],
        "status": AssetStatus.IN_REVIEW,
        "createdAt": clock.now_et(),  # Eastern wall-clock
        "createdByAgent": ctx.agent_id,
    }


def build_sources(payload: dict) -> list[Source]:
    """Coerce the model's `sources` into validated Source objects. An
    out-of-vocabulary sourceType is kept in sourceName and coerced to 'other'."""
    out: list[Source] = []
    for i, s in enumerate(payload.get("sources", []) or []):
        if not isinstance(s, dict):
            continue
        st = str(s.get("sourceType", "other")).strip().lower()
        if st not in _SOURCE_TYPES:
            st = "other"
        out.append(Source(
            sourceId=s.get("sourceId") or f"source-{i}",
            sourceType=st,
            sourceName=str(s.get("sourceName") or s.get("sourceType") or "source"),
            sourceAssetId=s.get("sourceAssetId"),
        ))
    return out


def str_list(payload: dict, key: str) -> list[str]:
    return [str(x) for x in (payload.get(key) or []) if str(x).strip()]


async def synthesize(ctx, *, upstream_ids: list[str], system_prompt: str,
                     schema: str, max_tokens: int = 4500) -> tuple[dict, dict]:
    """Gather approved upstream assets, ask the model for structured JSON, and
    return (payload, meta). meta carries title/version and the list of upstream
    assetIds available for claim tracing. payload is {} if the model output was
    unavailable/unparseable, so the agent can degrade cleanly.
    """
    b, title = brief(ctx)
    version = prior_version(ctx)

    blocks: list[str] = []
    upstream_asset_ids: list[str] = []
    for aid in upstream_ids:
        raw = ctx.input(aid)
        if not raw:
            continue
        asset_id = _parse(raw).get("assetId")
        if asset_id:
            upstream_asset_ids.append(asset_id)
        label = aid.replace("_", " ").upper()
        header = f"=== {label}" + (f" (assetId: {asset_id})" if asset_id else "") + " ==="
        blocks.append(f"{header}\n{raw}")

    context = "\n\n".join(blocks) or json.dumps(b, default=str) or ctx.topic
    user = f"{context}\n"
    if ctx.feedback:
        user += f"\n=== REVIEWER GUIDANCE (targeted revision) ===\n{ctx.feedback}\n"
    user += (
        "\n=== RULES ===\n"
        "1. Use ONLY the APPROVED upstream assets above. Never invent a figure "
        "you were not given.\n"
        "2. Trace every material claim to the assetId(s) above that support it.\n"
        "3. Separate evidence-backed statements from your own interpretation.\n"
        "4. Name assumptions and limitations explicitly; do not restate the inputs "
        "verbatim \u2014 synthesize.\n"
    )
    user += f"\nReturn ONLY JSON matching this schema:\n{schema}"

    text = await ctx.llm(system_prompt, user, max_tokens=max_tokens)
    payload = extract_json(text) or {}
    meta = {
        "title": title,
        "version": version,
        "brief": b,
        "upstream_asset_ids": upstream_asset_ids,
        "degraded": not bool(payload),
    }
    return payload, meta
