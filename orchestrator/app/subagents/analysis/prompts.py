"""Prompts for the Analysis agent (step 3).

WHY `rationale` IS SPELLED OUT
On the first run where every other agent came back clean, this one still recorded
three violations, all in `rationale`, all the same shape: "The request brief
identifies seven key questions and eight open questions…". The field description
used to be "why this analysis, grounded in the evidence", which is vague enough
that the model answered a different question — it narrated the REQUEST rather than
explaining how the evidence was weighed. Rule 9 in the shared instructions already
forbids that; the field had to stop inviting it.

A SECOND MODEL CALL DOES NOT FIX IT, AND THAT WAS MEASURED
The obvious next lever is to re-ask the model when the field comes back wrong. It
was tried on this agent and it failed: the re-ask genuinely rewrote the sentence and
landed in the same defect class — "Cold-start implications are mentioned in the
request brief as a key question but are not addressed in any of the four research
findings." Same defect before and after, $0.022 spent.

Recorded here because it is the useful negative result: when a model reaches for the
same wrong subject on the second attempt as on the first, asking again is not the
lever. The wording below is, and if that stops working the next step is a schema
change argued on its own merits.

AND WHY THE FIELD IS KEPT
Deleting `rationale` would be the easy way out — it is optional in the contract
(`contracts/analysis.py`) and nothing downstream reads it specifically. But read what
it actually produces: "All four research findings converge on the same layered
architecture and service selection framework. Documentation search, web search and
knowledge research all cite the same reference architectures, establishing high
confidence in the five-layer model." That is exactly the field's job, and no other
field carries it. The defect was ever only one trailing sentence. Removing three good
sentences to be rid of a fourth is a worse deliverable, not a better one.
"""

SYSTEM_PROMPT = (
    "You are the Analysis agent. Synthesize the approved request brief and the "
    "approved research findings into ONE cohesive analysis.\n\n"
    "Connect the objective and the evidence into a clear analysis with rationale. "
    "Every material claim must be traced to the upstream asset(s) that support it. "
    "Call out assumptions and evidence limitations honestly.\n\n"
    "`rationale` EXPLAINS HOW YOU WEIGHED THE EVIDENCE, and nothing else: which "
    "sources agreed, which disagreed and how you settled it, which findings carried "
    "the conclusion and which you set aside, and why a claim you marked medium or "
    "low confidence is not high. It is NOT a description of the request. Observed "
    "live, three times in one field: \"The request brief identifies seven key "
    "questions and eight open questions\u2026\" \u2014 true, and about the wrong "
    "subject. What the request leaves open belongs in `limitations`, which exists "
    "for it. If your rationale would read the same for any request on this topic, "
    "you have written about the wrong thing.\n\n"
    "Never invent a figure you were not given. Return ONLY valid JSON."
)

# The structured shape the model must return (mapped to Analysis).
SCHEMA = (
    '{"executiveSummary": "1-2 sentence reviewer-facing summary of the SUBJECT", '
    '"summary": "the core analysis", '
    '"rationale": "how you weighed the evidence: what agreed, what conflicted and '
    'how you settled it, what you set aside. NOT a description of the request", '
    '"claims": [{"statement": "a material claim", '
    '"tracedToAssetIds": ["asset-... ids from the inputs that support it"], '
    '"confidence": "high | medium | low"}], '
    '"assumptions": ["..."], "limitations": ["named evidence gaps, or []"], '
    '"sources": [{"sourceType": "research-finding | request-brief | other", '
    '"sourceName": "..."}]}'
)
