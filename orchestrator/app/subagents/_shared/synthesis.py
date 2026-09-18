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


def upstream_context(ctx, upstream_ids: list[str]) -> tuple[str, list[str]]:
    """The approved upstream assets AS THE MODEL SEES THEM, plus their assetIds.

    Extracted from `synthesize` so the regression corpus can build its `upstream`
    text through the SAME code the runner uses. It was hand-typed prose before,
    which hid a real defect: `json.dumps` escapes non-ASCII, so an en dash in an
    upstream asset reaches the model as `\\u2013` and a figure written "2\u20134
    weeks" could never be recognised as grounded. Paraphrasing the inputs in a
    fixture tests the paraphrase, not the pipeline.
    """
    blocks: list[str] = []
    ids: list[str] = []
    for aid in upstream_ids:
        raw = ctx.input(aid)
        if not raw:
            continue
        parsed = assets.parse(raw)
        asset_id = parsed.get("assetId")
        if asset_id:
            ids.append(asset_id)
        label = aid.replace("_", " ").upper()
        header = f"=== {label}" + (f" (assetId: {asset_id})" if asset_id else "") + " ==="
        blocks.append(f"{header}\n{assets.for_prompt(parsed) or raw}")
    return "\n\n".join(blocks), ids


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

    context, upstream_asset_ids = upstream_context(ctx, upstream_ids)
    # ensure_ascii=False for the same reason as assets.for_prompt: this fallback is
    # the grounding baseline when no upstream asset was approved, and escaped
    # characters make a faithfully quoted figure look invented.
    context = context or json.dumps(b, default=str, ensure_ascii=False) or ctx.topic
    user = f"{context}\n"
    if ctx.feedback:
        user += f"\n=== REVIEWER GUIDANCE (targeted revision) ===\n{ctx.feedback}\n"
    # These rules are SHORT on purpose, and they are about PROVENANCE rather than
    # about any subject matter: use the inputs, trace claims to them, separate
    # evidence from interpretation, invent nothing. That is the one editorial
    # standard a framework can fairly impose, because it is what makes a synthesized
    # asset auditable at all. Anything narrower — a house style, a length, a section
    # vocabulary — belongs to the workflow, so it belongs in your own agent's prompt
    # under app/subagents/<id>/prompts.py, not here.
    user += (
        "\n=== RULES ===\n"
        "1. Use ONLY the APPROVED upstream assets above.\n"
        "2. Trace every material claim to the assetId(s) above that support it.\n"
        "3. Separate evidence-backed statements from your own interpretation.\n"
        "4. Name assumptions and limitations explicitly; do not restate the inputs "
        "verbatim \u2014 synthesize. What the request leaves unsettled goes in "
        "`limitations`, which is FOR that; it is never a claim.\n"
        "5. No figure that is not in an upstream asset above — costs, thresholds, "
        "percentages, durations, cadences, timelines. Not even as an illustration. "
        "Say plainly when none was supplied.\n"
        "6. Do not invent a numbered or ordinal sequence to structure your own "
        "output. Carry the labels the inputs use, or order the work by dependency "
        "and say so in prose.\n"
        "7. Counts must match the lists they count, and one concern belongs in a "
        "list once.\n"
        "8. If an input says something is unavailable, it cannot become an action. "
        "Name the gap and what closing it would unblock.\n"
        "9. Write about the SUBJECT. Not about the request, not about the "
        "requester, not about this system's own run history \u2014 every reader "
        "downstream already has the request, and recalled or historical material "
        "orients you without ever being a claim or a rationale. A claim beginning "
        "'The request brief identifies\u2026' or 'The brief does not specify\u2026' "
        "or 'Eight prior runs completed\u2026' is about the wrong subject however "
        "true it is; move the substance into `limitations` and write the claim "
        "about the topic instead.\n"
    )
    user += f"\nReturn ONLY JSON matching this schema:\n{schema}"

    # max_tokens=None -> the agent's own `maxTokens` from workflow.json. A caller
    # may still override for one call, but no shipped agent needs to.
    payload = assets.extract_json(
        await ctx.llm(system_prompt, user, max_tokens=max_tokens)) or {}
    meta = {
        "title": title,
        "version": version,
        "brief": b,
        "upstream_asset_ids": upstream_asset_ids,
        "degraded": not bool(payload),
    }
    return payload, meta
