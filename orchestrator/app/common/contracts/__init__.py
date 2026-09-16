"""Asset contracts — the structured JSON shapes agents produce and persist.

Every agent emits a validated asset, so downstream agents and the UI can rely on
the shape instead of parsing prose. The five below are what THIS sample's agents
produce; they are examples, not a fixed set:

  * RequestBrief    — framing: what the run is trying to achieve
  * ResearchOutput  — evidence gathered from a tool, with provenance per finding
  * Analysis        — claims traced back to the assets that support them
  * Recommendation  — options with rationale and risks
  * Report          — the assembled, sectioned final asset

A customer's own contract goes in their agent's folder: subclass `AssetEnvelope`
for the shared provenance fields and pin `assetType` to whatever they call it.
Nothing here needs editing to add one — `assetType`, `sourceType` and
`sectionType` are all open strings for exactly that reason.
"""

from .base import (
    Artifact,
    ArtifactRole,
    ArtifactType,
    AssetEnvelope,
    AssetStatus,
    AssetType,
    Base,
    Source,
    SourceType,
)
from .brief import RequestBrief
from .research import EvidenceClass, Finding, ResearchOutput
from .analysis import Analysis, TracedClaim
from .recommendation import Recommendation, RecommendationItem
from .report import Report, ReportSection, SectionType

__all__ = [
    "Artifact",
    "ArtifactRole",
    "ArtifactType",
    "AssetEnvelope",
    "AssetStatus",
    "AssetType",
    "Base",
    "Source",
    "SourceType",
    "RequestBrief",
    "EvidenceClass",
    "Finding",
    "ResearchOutput",
    "Analysis",
    "TracedClaim",
    "Recommendation",
    "RecommendationItem",
    "Report",
    "ReportSection",
    "SectionType",
]
