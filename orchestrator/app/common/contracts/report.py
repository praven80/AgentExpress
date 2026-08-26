"""Report contract — the terminal (Report) asset.

Assembles the approved upstream assets into a final, sectioned report. Each
section is pinned to the upstream assetIds it draws on, and a completeness flag
records whether every expected section is present.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from .base import AssetEnvelope, AssetType, Base

SectionType = Literal[
    "executive-summary",
    "background",
    "findings",
    "analysis",
    "recommendations",
    "next-steps",
]

# The sections in presentation order — used to order output and detect gaps.
SECTION_ORDER: tuple[SectionType, ...] = (
    "executive-summary",
    "background",
    "findings",
    "analysis",
    "recommendations",
    "next-steps",
)


class ReportSection(Base):
    """One section of the assembled report."""

    section_id: str = Field(alias="sectionId")
    section_type: SectionType = Field(alias="sectionType")
    title: str
    content: str = ""
    source_asset_ids: list[str] = Field(default_factory=list, alias="sourceAssetIds")


class Report(AssetEnvelope):
    asset_type: Literal[AssetType.REPORT] = Field(
        default=AssetType.REPORT, alias="assetType"
    )

    title: str = ""
    sections: list[ReportSection] = Field(default_factory=list)
    is_complete: bool = Field(default=False, alias="isComplete")
