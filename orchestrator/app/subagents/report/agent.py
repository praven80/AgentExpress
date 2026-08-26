"""Report — assembles the final sectioned report (terminal step).

The terminal step (no HITL gate after it). It maps the approved upstream assets
into the standard report sections in canonical SECTION_ORDER, each pinned to the
upstream assetIds it draws on, and marks the report complete when every expected
section is present. The whole report is one versioned Report asset.
"""

from __future__ import annotations

import json

from app.common import synthesis
from app.common.base import Agent
from app.common.context import AgentContext
from app.common.contracts import SECTION_ORDER, Report, ReportSection

from .prompts import SCHEMA, SYSTEM_PROMPT

UPSTREAM = ["recommendation", "analysis", "intake"]

_SECTION_TYPES = set(SECTION_ORDER)
_ORDER = {t: i for i, t in enumerate(SECTION_ORDER)}


def _sections(payload: dict) -> list[ReportSection]:
    out: list[ReportSection] = []
    seen: set[str] = set()
    for s in payload.get("sections", []) or []:
        if not isinstance(s, dict):
            continue
        st = str(s.get("sectionType", "")).strip().lower()
        if st not in _SECTION_TYPES or st in seen:
            continue
        seen.add(st)
        out.append(ReportSection(
            sectionId=f"section-{st}",
            sectionType=st,
            title=str(s.get("title") or st.replace("-", " ").title()),
            content=str(s.get("content") or "").strip(),
            sourceAssetIds=[str(a) for a in (s.get("sourceAssetIds") or [])],
        ))
    out.sort(key=lambda s: _ORDER.get(s.section_type, 99))
    return out


class ReportAgent(Agent):
    system_prompt = SYSTEM_PROMPT

    async def run(self, ctx: AgentContext) -> str:
        payload, meta = await synthesis.synthesize(
            ctx, upstream_ids=UPSTREAM, system_prompt=SYSTEM_PROMPT, schema=SCHEMA, max_tokens=8000
        )
        sections = _sections(payload)
        present = {s.section_type for s in sections}
        is_complete = bool(sections) and all(t in present for t in SECTION_ORDER)

        asset = Report(
            **synthesis.envelope(ctx, meta, "report"),
            executiveSummary=str(payload.get("executiveSummary") or "").strip() or None,
            title=str(payload.get("title") or meta["title"]).strip(),
            sections=sections,
            isComplete=is_complete,
        )
        return json.dumps(asset.model_dump(by_alias=True, mode="json"), indent=2)


agent = ReportAgent()
