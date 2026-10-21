"""HTTP transport envelopes; business objects are defined only in schemas.py."""
from typing import Annotated

from pydantic import Field, model_validator

from app.schemas import (
    CaseData, Content, ContractModel, InfoUnitSubtype, InfoUnitType, Text, Title,
)


class CaseResponse(ContractModel):
    case: CaseData | None


class CasesResponse(ContractModel):
    cases: list[CaseData]
    current_case_id: str | None


class UpdateCaseInput(ContractModel):
    case_title: Text


EvidenceIds = Annotated[list[Text], Field(min_length=1)]


class CreateInfoUnitInput(ContractModel):
    type: InfoUnitType
    subtype: InfoUnitSubtype | None = None
    title: Title
    content: Content
    evidence_ids: EvidenceIds
    speaker: Text | None = None
    side: Text | None = None
    actor: Text | None = None
    target: Text | None = None
    related_issue: Text | None = None


class UpdateInfoUnitInput(ContractModel):
    type: InfoUnitType | None = None
    subtype: InfoUnitSubtype | None = None
    title: Title | None = None
    content: Content | None = None
    evidence_ids: EvidenceIds | None = None
    speaker: Text | None = None
    side: Text | None = None
    actor: Text | None = None
    target: Text | None = None
    related_issue: Text | None = None

    @model_validator(mode="after")
    def require_changes(self) -> "UpdateInfoUnitInput":
        if not self.model_fields_set:
            raise ValueError("At least one InfoUnit field must be provided")
        if "evidence_ids" in self.model_fields_set and self.evidence_ids is None:
            raise ValueError("evidence_ids must be a nonempty list when provided")
        return self


class ExtractionRunInput(ContractModel):
    case_id: Text


class RelationRunInput(ContractModel):
    case_id: Text
