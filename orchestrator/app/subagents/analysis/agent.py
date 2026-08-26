"""Analysis — synthesizes the brief and both research findings (step 3).

Combines the approved request brief and the two approved research outputs into a
single versioned Analysis: findings + rationale, with every material claim traced
to the upstream assets that support it, plus explicit assumptions and
limitations. On a revise cycle the reviewer's feedback arrives as ctx.feedback
and the version bumps, preserving prior versions.
"""

from __future__ import annotations

import json

from app.common import synthesis
from app.common.base import Agent
from app.common.context import AgentContext
from app.common.contracts import Analysis, TracedClaim

from .prompts import SCHEMA, SYSTEM_PROMPT

# The approved upstream assets this agent reads (the brief + both research outputs).
UPSTREAM = ["intake", "knowledge_research", "web_research"]

_CONFIDENCE = {"high", "medium", "low"}


def _claims(payload: dict) -> list[TracedClaim]:
    out: list[TracedClaim] = []
    for c in payload.get("claims", []) or []:
        if not isinstance(c, dict) or not c.get("statement"):
            continue
        conf = str(c.get("confidence", "")).strip().lower()
        out.append(TracedClaim(
            statement=str(c["statement"]),
            tracedToAssetIds=[str(a) for a in (c.get("tracedToAssetIds") or [])],
            confidence=conf if conf in _CONFIDENCE else None,
        ))
    return out


class AnalysisAgent(Agent):
    system_prompt = SYSTEM_PROMPT

    async def run(self, ctx: AgentContext) -> str:
        payload, meta = await synthesis.synthesize(
            ctx, upstream_ids=UPSTREAM, system_prompt=SYSTEM_PROMPT, schema=SCHEMA, max_tokens=6000
        )
        summary = str(payload.get("summary") or "").strip()
        limitations = synthesis.str_list(payload, "limitations")
        if meta["degraded"] or not summary:
            summary = summary or "Analysis could not be synthesised from the approved inputs."
            limitations.append("Model output was unavailable or unparseable for this run.")

        asset = Analysis(
            **synthesis.envelope(ctx, meta, "analysis"),
            executiveSummary=str(payload.get("executiveSummary") or "").strip() or None,
            summary=summary,
            rationale=str(payload.get("rationale") or "").strip(),
            claims=_claims(payload),
            assumptions=synthesis.str_list(payload, "assumptions"),
            limitations=limitations,
            sources=synthesis.build_sources(payload),
        )
        return json.dumps(asset.model_dump(by_alias=True, mode="json"), indent=2)


agent = AnalysisAgent()
