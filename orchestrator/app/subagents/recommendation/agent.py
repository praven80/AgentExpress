"""Recommendation — prioritized recommendations from the analysis (step 4).

Runs sequentially after Analysis. Turns the approved analysis (and the brief)
into a prioritized set of recommendations, each with its own rationale and any
associated risk, traced to the upstream assets. On a revise cycle the reviewer's
feedback arrives as ctx.feedback and the version bumps.
"""

from __future__ import annotations

import json

from app.common import synthesis
from app.common.base import Agent
from app.common.context import AgentContext
from app.common.contracts import Recommendation, RecommendationItem

from .prompts import SCHEMA, SYSTEM_PROMPT

UPSTREAM = ["analysis", "intake"]

_PRIORITY = {"high", "medium", "low"}


def _items(payload: dict) -> list[RecommendationItem]:
    out: list[RecommendationItem] = []
    for it in payload.get("items", []) or []:
        if not isinstance(it, dict) or not it.get("title"):
            continue
        pri = str(it.get("priority", "")).strip().lower()
        out.append(RecommendationItem(
            title=str(it["title"]),
            detail=str(it.get("detail") or "").strip(),
            priority=pri if pri in _PRIORITY else None,
            rationale=str(it.get("rationale") or "").strip(),
            tracedToAssetIds=[str(a) for a in (it.get("tracedToAssetIds") or [])],
        ))
    return out


class RecommendationAgent(Agent):
    system_prompt = SYSTEM_PROMPT

    async def run(self, ctx: AgentContext) -> str:
        payload, meta = await synthesis.synthesize(
            ctx, upstream_ids=UPSTREAM, system_prompt=SYSTEM_PROMPT, schema=SCHEMA, max_tokens=6000
        )
        items = _items(payload)
        summary = str(payload.get("summary") or "").strip()
        risks = synthesis.str_list(payload, "risks")
        if meta["degraded"] or not items:
            summary = summary or "Recommendations could not be synthesised from the approved inputs."
            risks.append("Model output was unavailable or unparseable for this run.")

        asset = Recommendation(
            **synthesis.envelope(ctx, meta, "recommendation"),
            executiveSummary=str(payload.get("executiveSummary") or "").strip() or None,
            summary=summary,
            items=items,
            risks=risks,
            assumptions=synthesis.str_list(payload, "assumptions"),
            sources=synthesis.build_sources(payload),
        )
        return json.dumps(asset.model_dump(by_alias=True, mode="json"), indent=2)


agent = RecommendationAgent()
