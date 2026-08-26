"""Shared runner for the step-2 research sub-agents.

The two research agents (knowledge_research over the Knowledge Base, and
web_research over an MCP server) share one flow:

  1. take the approved request brief (from the intake agent upstream),
  2. gather grounding evidence — Knowledge Base retrieval (RAG) and/or a Gateway
     MCP server, depending on how the agent is configured,
  3. ask the model for a structured analysis with every finding classified by
     evidence type and every data limitation named,
  4. assemble and validate a ResearchOutput asset (contract-adherent).

This is framework plumbing (like ctx.llm); each agent keeps only its own
identity — its prompt and its data source.
"""

from __future__ import annotations

import json
import re

from app.common import clock
from app.common.contracts import EvidenceClass, Finding, ResearchOutput
from app.common.contracts.base import AssetStatus, Source, SourceType

_EVIDENCE = set(EvidenceClass.__args__)
_SOURCE_TYPES = set(SourceType.__args__)

_SCHEMA = (
    '{"summary": "...", '
    '"findings": [{"statement": "...", '
    '"classification": "sourced-fact | calculation | assumption | agent-interpretation", '
    '"sourceRef": "which source supplied it"}], '
    '"dataLimitations": ["named unavailable/incomplete/unsupported evidence, or [] if none"], '
    '"sources": [{"sourceType": "knowledge-base | mcp-tool | calculation | '
    'assumption | other", "sourceName": "...", "sourceAssetId": "optional"}]}'
)


def _slug(text: str, limit: int = 48) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:limit] or "request"


def _extract_json(text: str) -> dict | None:
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    start, end = t.find("{"), t.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        return json.loads(t[start : end + 1])
    except (ValueError, TypeError):
        return None


def _brief(ctx) -> dict:
    """The approved request-brief asset from intake, parsed."""
    raw = ctx.input("intake")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {}


def _prior_version(ctx) -> int:
    """This agent's own previous output version (for the revise cycle)."""
    raw = ctx.input(ctx.agent_id)
    if not raw:
        return 1
    try:
        return int(json.loads(raw).get("version", 1)) + 1
    except (TypeError, ValueError):
        return 1


def _findings(payload: dict) -> list[Finding]:
    out: list[Finding] = []
    for f in payload.get("findings", []) or []:
        if not isinstance(f, dict) or not f.get("statement"):
            continue
        cls = str(f.get("classification", "")).strip().lower()
        if cls not in _EVIDENCE:
            cls = "agent-interpretation"  # unclassified -> the most cautious label
        out.append(Finding(statement=str(f["statement"]), classification=cls,
                           sourceRef=f.get("sourceRef")))
    return out


def _sources(payload: dict) -> list[Source]:
    out: list[Source] = []
    for i, s in enumerate(payload.get("sources", []) or []):
        if not isinstance(s, dict):
            continue
        st = str(s.get("sourceType", "other")).strip().lower()
        if st not in _SOURCE_TYPES:
            st = "other"  # keep the real provider in sourceName, coerce the enum
        out.append(Source(
            sourceId=s.get("sourceId") or f"source-{i}",
            sourceType=st,
            sourceName=str(s.get("sourceName") or s.get("sourceType") or "source"),
            sourceAssetId=s.get("sourceAssetId"),
        ))
    return out


async def synthesize(ctx, *, system_prompt: str,
                     mcp_label: str | None = None, use_rag: bool = True,
                     kb_filter: str | None = None) -> str:
    """Run the research flow and return the validated ResearchOutput as JSON.

    Data access is per-agent: set `use_rag=True` to retrieve from the Bedrock
    Knowledge Base (RAG), and/or `mcp_label` to call a Gateway MCP server. An
    MCP-only agent passes use_rag=False; a RAG-only agent passes no mcp_label.
    Both degrade to a simulated response with a noted data limitation when the
    backend is not wired.

    `kb_filter` scopes RAG retrieval to a single corpus by `doc_type` (the KB is
    one shared index; each doc is tagged with its folder). This keeps a research
    agent's evidence within its own document set.
    """
    brief = _brief(ctx)
    brief_text = json.dumps(brief, default=str) if brief else ctx.topic
    query = (brief.get("objective") or brief.get("title") or brief_text)[:200]

    evidence_parts: list[str] = []
    limitations: list[str] = []

    if use_rag:
        kb_text, kb_mode = await ctx.retrieve(query, doc_type=kb_filter)
        if kb_text:
            evidence_parts.append(f"=== KNOWLEDGE BASE (mode={kb_mode}) ===\n{kb_text}")
        if kb_mode == "simulated":
            limitations.append("Knowledge Base returned simulated grounding (not live data).")

    if mcp_label:
        mcp_text, mcp_mode = await ctx.mcp(mcp_label, query)
        if mcp_text:
            evidence_parts.append(f"=== {mcp_label.upper()} (mode={mcp_mode}) ===\n{mcp_text}")
        if mcp_mode == "simulated":
            limitations.append(f"{mcp_label} data is simulated for this run (provider not live).")

    evidence = "\n\n".join(evidence_parts)
    user = f"=== APPROVED REQUEST BRIEF ===\n{brief_text}\n"
    if evidence:
        user += f"\n{evidence}\n"
    if ctx.feedback:
        user += f"\n=== REVIEWER GUIDANCE ===\n{ctx.feedback}\n"
    user += (
        "\n=== HOW TO USE THESE INPUTS ===\n"
        "1. The REQUEST BRIEF above is the APPROVED, AUTHORITATIVE input. Treat it "
        "as complete and current.\n"
        "2. Label a finding 'sourced-fact' ONLY when the value appears in an "
        "EVIDENCE block above. If a value is not present in these inputs, do not "
        "assert it; use 'assumption' or 'agent-interpretation', or omit it. Every "
        "finding's sourceRef must name an input actually shown above.\n"
        "3. Do NOT echo the brief back as findings. A finding must be YOUR domain "
        "analysis for this request, or a fact drawn from a retrieved EVIDENCE block "
        "above. Prefer 3-8 meaningful findings over a long list.\n"
        "4. dataLimitations: list ONLY genuinely missing evidence relevant to your "
        "job (e.g. a provider returned no data, or the Knowledge Base had no "
        "relevant documents). An EVIDENCE block marked 'simulated' or empty means "
        "that source is not available for this run \u2014 a valid limitation. Return "
        "[] if you had what you needed.\n"
    )
    user += f"\nReturn ONLY JSON matching this schema:\n{_SCHEMA}"

    # Research emits a full structured JSON (summary + several classified findings
    # + sources + limitations); the default per-agent budget would truncate it
    # mid-JSON and make it unparseable, so request a generous budget.
    text = await ctx.llm(system_prompt, user, max_tokens=4000)
    payload = _extract_json(text) or {}

    findings = _findings(payload)
    sources = _sources(payload)
    data_limits = [str(x) for x in (payload.get("dataLimitations") or [])] + limitations
    summary = str(payload.get("summary") or "").strip()
    if not summary and not findings:
        # Model unavailable / unparseable (e.g. simulated local). Degrade to a
        # well-formed asset that says so, rather than failing the whole group.
        summary = "Research could not be synthesised from available evidence."
        data_limits.append("Model output was unavailable or unparseable for this run.")

    title = brief.get("title") or ctx.topic or ctx.agent_id
    version = _prior_version(ctx)

    asset = ResearchOutput(
        assetId=f"asset-research-{ctx.agent_id}-{_slug(str(title))}-v{version}",
        version=version,
        status=AssetStatus.IN_REVIEW,
        createdAt=clock.now_et(),  # Eastern wall-clock
        createdByAgent=ctx.agent_id,
        executiveSummary=summary or None,
        summary=summary,
        findings=findings,
        dataLimitations=data_limits,
        sources=sources,
    )
    return json.dumps(asset.model_dump(by_alias=True, mode="json"), indent=2)
