"""Shared asset envelope and controlled vocabulary.

Every asset an agent produces is structured JSON validated by a Pydantic model.
They share one envelope (`AssetEnvelope`) and a small controlled vocabulary, so
the whole pipeline emits consistent, machine-checkable output.

Two conventions worth knowing before editing:

1. The JSON is camelCase; a database would be snake_case. Models are declared in
   snake_case with camelCase aliases, and `populate_by_name=True` lets both work.
   Serialise with `by_alias=True` when producing the customer-shaped JSON.

2. `status` in the JSON is lowercase-hyphenated ('in-review', 'final') and is the
   asset's own payload status. It is distinct from the orchestrator's workflow
   readiness label, which never goes in the payload.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Base(BaseModel):
    """Shared config. Extra fields are forbidden on purpose.

    The contract IS the payload. If an asset arrives with a field we have not
    modelled, failing loudly is better than silently dropping data the
    orchestrator would then never persist.
    """

    model_config = ConfigDict(
        populate_by_name=True,
        extra="forbid",
        use_enum_values=False,
        str_strip_whitespace=True,
    )


# ---------------------------------------------------------------------------
# Controlled vocabulary
# ---------------------------------------------------------------------------


class AssetType(str, Enum):
    """The canonical asset types, one per pipeline stage."""

    REQUEST_BRIEF = "request-brief"
    RESEARCH_FINDING = "research-finding"
    ANALYSIS = "analysis"
    RECOMMENDATION = "recommendation"
    REPORT = "report"


class AssetStatus(str, Enum):
    """The asset's own `status` field.

    Distinct from any workflow state the orchestrator tracks separately.
    """

    PENDING = "pending"
    IN_REVIEW = "in-review"
    APPROVED = "approved"
    FINAL = "final"


# Kept as constrained strings rather than Enums: new artifact/source kinds are
# likely to appear over time.
ArtifactType = Literal[
    "chart",
    "document",
    "link",
    "pdf",
    "table",
]

ArtifactRole = Literal["final", "supporting-artifact"]

SourceType = Literal[
    # external inputs / grounding
    "knowledge-base",
    "mcp-tool",
    "user-input",
    # internal assets
    "request-brief",
    "research-finding",
    "analysis",
    "recommendation",
    # provenance of a derived claim
    "calculation",
    "assumption",
    "other",
]


# ---------------------------------------------------------------------------
# Shared components
# ---------------------------------------------------------------------------


class Artifact(Base):
    """A concrete file or record. Distinct from an asset, which is structured JSON."""

    artifact_id: str = Field(alias="artifactId")
    name: str
    artifact_type: ArtifactType = Field(alias="artifactType")
    artifact_role: ArtifactRole | None = Field(default=None, alias="artifactRole")
    uri: str | None = None
    description: str | None = None


class Source(Base):
    """One provenance entry.

    Points at another asset (`source_asset_id`), an artifact (`artifact_id`), or
    neither - a live tool call or a calculation has no file behind it.
    """

    source_id: str = Field(alias="sourceId")
    source_type: SourceType = Field(alias="sourceType")
    source_name: str = Field(alias="sourceName")
    source_asset_id: str | None = Field(default=None, alias="sourceAssetId")
    artifact_id: str | None = Field(default=None, alias="artifactId")
    description: str | None = None

    @model_validator(mode="after")
    def _not_both_targets(self) -> "Source":
        if self.source_asset_id and self.artifact_id:
            raise ValueError(
                f"source {self.source_id!r} points at both an asset and an artifact; "
                "a source resolves to at most one target"
            )
        return self


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------


class AssetEnvelope(Base):
    """Fields common to every asset."""

    asset_id: str = Field(alias="assetId")
    asset_type: AssetType = Field(alias="assetType")
    version: int = Field(ge=1)
    status: AssetStatus
    created_at: datetime = Field(alias="createdAt")
    created_by_agent: str = Field(alias="createdByAgent")

    executive_summary: str | None = Field(default=None, alias="executiveSummary")
    source_asset_ids: list[str] = Field(default_factory=list, alias="sourceAssetIds")
    artifacts: list[Artifact] = Field(default_factory=list)
    sources: list[Source] = Field(default_factory=list)
