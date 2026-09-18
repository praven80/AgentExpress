"""The asset-type vocabulary THIS SAMPLE's five contracts use.

It lives here, not in `app.common.contracts`, because the names are a property of
this workflow: a claims pipeline has no "research-finding" and a triage pipeline has
no "report". The framework only requires that `AssetEnvelope.asset_type` be a
string; it never compares it against a list.

Kept as an Enum so the five contracts below cannot drift apart in spelling, and so
each can pin itself strictly with `Literal[AssetType.X]`. Your own contract does not
need an entry here — a plain `Literal["claim-decision"]` is enough.
"""

from enum import StrEnum


class AssetType(StrEnum):
    REQUEST_BRIEF = "request-brief"
    RESEARCH_FINDING = "research-finding"
    ANALYSIS = "analysis"
    RECOMMENDATION = "recommendation"
    REPORT = "report"
