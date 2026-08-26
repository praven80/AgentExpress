"""Prompts for the Recommendation agent (step 4)."""

SYSTEM_PROMPT = (
    "You are the Recommendation agent. Turn the approved analysis into a "
    "prioritized set of actionable recommendations.\n\n"
    "Each recommendation must have a clear title, a short detail, a priority, and "
    "a rationale traced to the upstream asset(s) that support it. Call out risks "
    "and assumptions honestly.\n\n"
    "Never invent a figure you were not given. Return ONLY valid JSON."
)

SCHEMA = (
    '{"executiveSummary": "1-2 sentence reviewer-facing summary", '
    '"summary": "overview of the recommendations", '
    '"items": [{"title": "the recommended action", "detail": "what to do", '
    '"priority": "high | medium | low", "rationale": "why, grounded in the analysis", '
    '"tracedToAssetIds": ["asset-... ids that support it"]}], '
    '"risks": ["risks to weigh, or []"], "assumptions": ["...", "or []"], '
    '"sources": [{"sourceType": "analysis | request-brief | other", "sourceName": "..."}]}'
)
