"""Request brief contract — the step-1 (Intake) output.

Turns the user's free-text request into a structured brief the rest of the
pipeline can consume: a clear objective, the scope, the specific questions to
answer, any constraints, and the open questions a human should confirm.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from .base import AssetEnvelope, AssetType, Base


class RequestBrief(AssetEnvelope):
    asset_type: Literal[AssetType.REQUEST_BRIEF] = Field(
        default=AssetType.REQUEST_BRIEF, alias="assetType"
    )

    title: str
    objective: str = ""
    scope: str = ""
    key_questions: list[str] = Field(default_factory=list, alias="keyQuestions")
    constraints: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list, alias="openQuestions")
