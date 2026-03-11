"""Pydantic schemas for request/response validation."""

from pydantic import BaseModel, Field


class ReasonRequest(BaseModel):
    """Input payload for reasoning endpoint."""

    case_text: str = Field(..., min_length=1)
    question: str = Field(..., min_length=1)


class ReasonResponse(BaseModel):
    """Structured reasoning output consumed by frontend."""

    entities: list[dict] = Field(default_factory=list)
    events: list[dict] = Field(default_factory=list)
    claims: list[dict] = Field(default_factory=list)
    conflicts: list[dict] = Field(default_factory=list)
    evidence_paths: list[dict] = Field(default_factory=list)
    recommended_view: str = "conflict_compare"
    summary: str = ""
