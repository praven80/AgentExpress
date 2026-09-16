"""Shared runner for the research sub-agents.

Every research agent shares one flow, regardless of where its evidence comes
from — the difference is one line of config (its `tool`), not code:

  1. take the approved request brief (from the intake agent upstream),
  2. gather grounding evidence from the tool the agent is bound to (Knowledge
     Base, Web Search, a remote MCP server, or a REST API), or none at all,
  3. ask the model for a structured analysis with every finding classified by
     evidence type and every data limitation named,
  4. assemble and validate a ResearchOutput asset (contract-adherent).

This is framework plumbing (like ctx.llm); each agent keeps only its own
identity — its prompt and its data source.
"""

from __future__ import annotations

import json
import re

from app.common import assets, clock
from app.common.config import TOOLS
from app.common.contracts import EvidenceClass, Finding, ResearchOutput
from app.common.contracts.base import AssetStatus
from app.common.errors import ModelOutputUnusable

_EVIDENCE = set(EvidenceClass.__args__)

# How each tool type is labelled in the evidence block handed to the model, so the
# model can attribute a finding to the right kind of source.
_EVIDENCE_LABELS = {
    "kb": "KNOWLEDGE BASE",
    "websearch": "WEB SEARCH",
    "mcp": "MCP SERVER",
    "openapi": "REST API",
    # A customer's own Lambda, fronting whatever the Gateway cannot reach directly
    # (a warehouse, an internal service, something inside a VPC). The label is
    # deliberately generic: only the customer knows what is behind it, and naming
    # the transport is more honest than guessing at the source.
    "lambda": "TOOL FUNCTION",
}
# The JSON shape asked of the model. NOT a parameter: it is the wire form of the
# ResearchOutput contract this module returns, and the parsing below (_findings,
# assets.build_sources, summary, dataLimitations) is its other half. Changing one without the
# other yields ModelOutputUnusable. To emit a DIFFERENT shape, write your own
# `run()` in app/subagents/<id>/agent.py and return your own contract — nothing
# obliges an agent to use this runner.
RESEARCH_SCHEMA = (
    '{"summary": "...", '
    '"findings": [{"statement": "...", '
    '"classification": "sourced-fact | calculation | assumption | agent-interpretation", '
    '"sourceRef": "which source supplied it"}], '
    '"dataLimitations": ["named unavailable/incomplete/unsupported evidence, or [] if none"], '
    '"sources": [{"sourceType": "the kind of source", '
    '"sourceName": "the title of the source, verbatim", '
    '"url": "the url EXACTLY as shown in the evidence block, or omit if it had none", '
    '"sourceAssetId": "optional"}]}'
)

# How to use the inputs. Domain-neutral on purpose — it talks about evidence,
# provenance and citations, not about any particular subject matter. Override it
# per agent from app/subagents/<id>/prompts.py when your domain needs different
# wording:
#
#     research.synthesize(ctx, system_prompt=SYSTEM, instructions=MY_INSTRUCTIONS)
#
# Keep whatever your version says about citations: the URL rules below are not
# style, they are what makes the citation verification downstream meaningful.
RESEARCH_INSTRUCTIONS = (
    "\n=== HOW TO USE THESE INPUTS ===\n"
    "1. The REQUEST above is the APPROVED, AUTHORITATIVE input. Treat it as "
    "complete and current.\n"
    "2. Label a finding 'sourced-fact' ONLY when the value appears in an "
    "EVIDENCE block above. If a value is not present in these inputs, do not "
    "assert it; use 'assumption' or 'agent-interpretation', or omit it. Every "
    "finding's sourceRef must name an input actually shown above.\n"
    "3. Do NOT echo the request back as findings. A finding must be YOUR domain "
    "analysis for this request, or a fact drawn from a retrieved EVIDENCE block "
    "above. Prefer 3-8 meaningful findings over a long list.\n"
    "3a. CITATIONS. Each evidence item is numbered and may carry 'title:', "
    "'url:' and 'published:'. When a finding comes from one, its sourceRef MUST "
    "be that item's url when it has one (copy it character for character), or "
    "its title when it does not. NEVER write a url that is not shown above — "
    "not even a plausible-looking one. Reproduce every url you were given in "
    "`sources` so it can be displayed to the reader; omit `url` for an item "
    "that had none.\n"
    "4. dataLimitations: list ONLY genuinely missing evidence relevant to your "
    "job (e.g. the tool returned no relevant documents for this query). Return "
    "[] if you had what you needed.\n"
)


def _findings(payload: dict, evidence: str = "") -> list[Finding]:
    """Coerce the model's `findings` into validated Finding objects.

    A sourceRef that is a URL is verified against the evidence, same as in
    assets.build_sources: an unverifiable link is replaced with an explicit marker
    rather than passed on as though it were a real citation. A finding claiming to be a
    sourced-fact on the strength of an invented URL is also downgraded, since its
    provenance cannot be established.
    """
    out: list[Finding] = []
    for f in payload.get("findings", []) or []:
        if not isinstance(f, dict) or not f.get("statement"):
            continue
        cls = str(f.get("classification", "")).strip().lower()
        if cls not in _EVIDENCE:
            cls = "agent-interpretation"  # unclassified -> the most cautious label
        ref = f.get("sourceRef")
        ref_s = str(ref or "")
        if evidence and "http" in ref_s:
            for url in re.findall(r"https?://[^\s\"'<>）)]+", ref_s):
                if url not in evidence:
                    ref_s = ref_s.replace(url, "[unverifiable link removed]")
                    if cls == "sourced-fact":
                        cls = "agent-interpretation"
            ref = ref_s
        out.append(Finding(statement=str(f["statement"]), classification=cls,
                           sourceRef=ref))
    return out


async def synthesize(ctx, *, system_prompt: str,
                     instructions: str = RESEARCH_INSTRUCTIONS) -> str:
    """Run the evidence-gathering flow and return a validated ResearchOutput as JSON.

    Data access is entirely CONFIG-driven: the agent's `tool` field in
    workflow.json names an entry in the `tools` block, and that entry's `type`
    decides how the call is made — Knowledge Base retrieval, the managed Web
    Search connector, a remote MCP server, an OpenAPI-described REST API, or your
    own Lambda. For a Knowledge Base tool the agent's `corpus` scopes retrieval to
    one document set.

    An agent with no `tool` reasons purely over its upstream inputs. A tool that
    cannot be called RAISES (ToolUnavailable / ToolDenied) — the run fails with the
    reason on the failing agent rather than continuing without the evidence.

    So a new agent of this kind needs a folder and a workflow.json entry, nothing
    here. What you CAN vary from your agent folder:
      * `system_prompt`  — who the agent is (app/subagents/<id>/prompts.py)
      * `instructions`   — how it should use the inputs; defaults to the
                           domain-neutral RESEARCH_INSTRUCTIONS above
    What you cannot vary here is the OUTPUT SHAPE, which is the ResearchOutput
    contract. To emit something else, write your own `run()` and return your own
    contract — this runner is a convenience, not a requirement.
    """
    brief = assets.brief(ctx)
    brief_text = json.dumps(brief, default=str) if brief else ctx.topic
    query = assets.brief_query(brief, brief_text)

    evidence_parts: list[str] = []
    limitations: list[str] = []

    tool_key = getattr(ctx, "tool", None)
    if tool_key:
        spec = TOOLS.get(tool_key) or {}
        kind = str(spec.get("type", "mcp")).lower()
        label = _EVIDENCE_LABELS.get(kind, "TOOL")

        if kind == "kb":
            corpus = getattr(ctx, "corpus", None)
            text, mode = await ctx.retrieve(query, doc_type=corpus)
            scope = f", corpus={corpus}" if corpus else ""
        else:
            text, mode = await ctx.call_tool(tool_key, query)
            scope = ""

        # A failed call raises, so reaching here means the tool answered.
        if text:
            evidence_parts.append(f"=== {label}: {tool_key} (mode={mode}{scope}) ===\n{text}")
        else:
            # The tool ran and legitimately had nothing to say. That is real
            # information, not a failure — name it so the model does not invent.
            limitations.append(
                f"'{tool_key}' returned no matching results for this query.")

    evidence = "\n\n".join(evidence_parts)
    user = f"=== APPROVED REQUEST ===\n{brief_text}\n"
    if evidence:
        user += f"\n{evidence}\n"
    if ctx.feedback:
        user += f"\n=== REVIEWER GUIDANCE ===\n{ctx.feedback}\n"
    user += instructions
    user += f"\nReturn ONLY JSON matching this schema:\n{RESEARCH_SCHEMA}"

    # No max_tokens override: the budget is the agent's own `maxTokens` from
    # workflow.json (see app/orchestrator/registry.py). Research emits a full
    # structured JSON — summary + several classified findings + sources +
    # limitations — so too small a budget truncates it mid-JSON and it fails to
    # parse. Raise that agent's maxTokens rather than editing this line.
    text = await ctx.llm(system_prompt, user)
    payload = assets.extract_json(text) or {}

    findings = _findings(payload, evidence)
    # `evidence` is passed so a citation URL can be verified against what the model
    # was actually shown, rather than trusted.
    sources = assets.build_sources(payload, verify_urls_against=evidence)
    data_limits = assets.str_list(payload, "dataLimitations") + limitations
    summary = str(payload.get("summary") or "").strip()
    if not summary and not findings:
        # The model answered but produced nothing usable. Fail rather than emit an
        # empty asset that looks like a completed research step.
        raise ModelOutputUnusable(
            f"{ctx.agent_id}: the model returned no parseable research JSON "
            f"({len(text or '')} chars). Raising rather than emitting an empty asset, "
            f"which downstream agents would treat as real findings.")

    title = assets.brief_title(brief, ctx)
    version = assets.prior_version(ctx)

    asset = ResearchOutput(
        assetId=f"asset-research-{ctx.agent_id}-{assets.slug(str(title))}-v{version}",
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
