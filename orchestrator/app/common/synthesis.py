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
from typing import Any

from app.common import assets, clock
from app.common.contracts.base import AssetStatus

# Re-exported so a synthesis agent has ONE import. The implementations live in
# app/common/assets.py because research.py needs the same five mechanics — they
# were duplicated in both runners, and `extract_json` had already diverged.
brief = assets.brief
build_sources = assets.build_sources
extract_json = assets.extract_json
prior_version = assets.prior_version
slug = assets.slug
str_list = assets.str_list


def envelope(ctx, meta: dict, asset_type_slug: str) -> dict[str, Any]:
    """AssetEnvelope kwargs (by alias) shared by every synthesized asset."""
    return {
        "assetId": f"asset-{asset_type_slug}-{slug(meta['title'])}-v{meta['version']}",
        "version": meta["version"],
        "status": AssetStatus.IN_REVIEW,
        "createdAt": clock.now_et(),  # Eastern wall-clock
        "createdByAgent": ctx.agent_id,
    }


async def synthesize(ctx, *, upstream_ids: list[str], system_prompt: str,
                     schema: str, max_tokens: int | None = None) -> tuple[dict, dict]:
    """Gather approved upstream assets, ask the model for structured JSON, and
    return (payload, meta). meta carries title/version and the list of upstream
    assetIds available for claim tracing. payload is {} if the model output was
    unavailable/unparseable, so the agent can degrade cleanly.
    """
    b = assets.brief(ctx)
    title = assets.brief_title(b, ctx)
    version = assets.prior_version(ctx)

    blocks: list[str] = []
    upstream_asset_ids: list[str] = []
    for aid in upstream_ids:
        raw = ctx.input(aid)
        if not raw:
            continue
        asset_id = assets.parse(raw).get("assetId")
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

    # max_tokens=None -> the agent's own `maxTokens` from workflow.json. A caller
    # may still override for one call, but no shipped agent needs to.
    text = await ctx.llm(system_prompt, user, max_tokens=max_tokens)
    payload = assets.extract_json(text) or {}
    meta = {
        "title": title,
        "version": version,
        "brief": b,
        "upstream_asset_ids": upstream_asset_ids,
        "degraded": not bool(payload),
    }
    return payload, meta
