"""Prompts for the Report agent (terminal step).

SECTIONS is the single source of truth for this report's shape: the system prompt,
the JSON schema and the agent's ordering + completeness check are all built from
it. Change the tuple to change the report — one edit, inside this agent's own
folder, with nothing in the framework to touch.
"""

# The sections this report has, in presentation order. A section the model returns
# that is NOT listed here is still kept (it sorts after these) — see agent._sections.
SECTIONS: tuple[str, ...] = (
    "executive-summary",
    "background",
    "findings",
    "analysis",
    "recommendations",
    "next-steps",
)

_SECTION_LIST = ", ".join(SECTIONS)

SYSTEM_PROMPT = (
    "You are the Report agent. Assemble the approved upstream assets into a clear, "
    "sectioned final report.\n\n"
    f"Produce these sections in order: {_SECTION_LIST}. Each section draws on the "
    "relevant upstream asset(s); pin the assetIds it uses. Write for a reader who "
    "has not seen the intermediate assets.\n\n"
    "Never invent a figure you were not given. Return ONLY valid JSON."
)

SCHEMA = (
    '{"title": "the report title", '
    '"executiveSummary": "1-2 sentence summary", '
    '"sections": [{"sectionType": "' + " | ".join(SECTIONS) + '", "title": "...", '
    '"content": "the section prose", '
    '"sourceAssetIds": ["asset-... ids this section draws on"]}]}'
)
