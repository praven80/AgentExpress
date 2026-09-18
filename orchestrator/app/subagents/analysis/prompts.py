"""Prompts for the Analysis agent (step 3).

WHY `rationale` IS SPELLED OUT
On the first run where every other agent came back clean, this one still recorded
three violations, all in `rationale`, all the same shape: "The request brief
identifies seven key questions and eight open questions…". The field description
used to be "why this analysis, grounded in the evidence", which is vague enough
that the model answered a different question — it narrated the REQUEST rather than
explaining how the evidence was weighed. Rule 9 in the shared instructions already
forbids that; the field had to stop inviting it.

AND WHY REPAIR IS *NOT* ENABLED FOR THIS AGENT, HAVING BEEN TRIED
The defect survived three revisions of the wording below while all seven other
agents came back clean, which looked like the signal that prompting had run out. So
`outputRules.repair` was turned on for this agent alone — one extra model call, only
when a check fails — and it was measured.

It did not work. Repair fired, the re-ask genuinely rewrote the field (the sentence
changed), and the rewrite landed in the same defect class: "Cold-start implications
are mentioned in the request brief as a key question but are not addressed in any of
the four research findings." One violation before, one after, $0.022 spent. The
retry is kept only when it has strictly fewer violations and introduces no new kind,
so it was kept — and it bought nothing.

The flag is therefore reverted, and this note is the reason it should not be tried
again without new evidence. When a model reaches for the same wrong subject on the
second attempt as on the first, the re-ask is not the lever either.

AND WHY THE FIELD IS KEPT ANYWAY, WITH THE VIOLATION RECORDED
The obvious next step is to delete `rationale` from the schema below — it is
optional in the contract (`contracts/analysis.py`), nothing downstream reads it
specifically, and removing it would take the violation count to zero.

Reading what it actually produced argues against that. On the fourth run it opened:
"All four research findings converge on the same layered architecture and service
selection framework. Documentation search, web search and knowledge research all
cite the same AWS reference architectures, establishing high confidence in the
five-layer model." That is exactly the field's job, and it is information no other
field carries. The violation was ONE trailing sentence about which key questions the
evidence covered.

So this one is left recorded rather than removed, and that is the output-rules layer
working as designed: a visible note on the asset telling a reviewer which sentence
to distrust, on a field whose other three sentences are worth having. Deleting good
content to clear a counter would be the check corrupting the deliverable — the same
mistake as suppressing a useful recommendation to satisfy `action-on-unavailable`.
Four prompt revisions and one measured repair attempt is enough; the next lever, if
anyone wants one, is a schema change and should be argued on its own merits.
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
