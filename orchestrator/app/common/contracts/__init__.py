"""Asset contracts — the structured JSON shapes agents produce and persist.

One contract per pipeline stage, so every agent emits consistent, validated
output that downstream agents (and the UI) can rely on:

  * Step 1  Intake          -> RequestBrief
  * Step 2  Research (x2)    -> ResearchOutput
  * Step 3  Analysis         -> Analysis
  * Step 4  Recommendation   -> Recommendation
  * Step 5  Report           -> Report
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
from .report import SECTION_ORDER, Report, ReportSection, SectionType

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
    "SECTION_ORDER",
    "Report",
    "ReportSection",
    "SectionType",
]
