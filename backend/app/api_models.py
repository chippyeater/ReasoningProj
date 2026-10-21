"""HTTP transport envelopes; business objects are defined only in schemas.py."""
from enum import Enum
from typing import Annotated, Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

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


class InfoUnitAgentAction(str, Enum):
    create = "create"
    update = "update"


class InfoUnitAgentRequest(ContractModel):
    action: InfoUnitAgentAction
    instruction: Annotated[str, Field(min_length=1, max_length=2000, pattern=r"\S")]
    target_info_unit_id: Text | None = None

    @model_validator(mode="after")
    def check_target(self) -> "InfoUnitAgentRequest":
        if self.action == InfoUnitAgentAction.create and self.target_info_unit_id is not None:
            raise ValueError("create must not specify target_info_unit_id")
        if self.action == InfoUnitAgentAction.update and self.target_info_unit_id is None:
            raise ValueError("update requires target_info_unit_id")
        return self


class InfoUnitAgentDraft(ContractModel):
    type: InfoUnitType
    subtype: InfoUnitSubtype | None
    title: Title
    content: Content
    evidence_ids: EvidenceIds
    speaker: Text | None
    side: Text | None
    actor: Text | None
    target: Text | None
    related_issue: Text | None

    @field_validator("title", "content")
    @classmethod
    def reject_surrounding_whitespace(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("text must not have leading or trailing whitespace")
        return value


class ModelInfoUnitAgentProposal(ContractModel):
    """Strict provider boundary. This is a proposal and has no writable IDs or SourceRefs."""

    model_config = ConfigDict(extra="forbid", strict=True)
    action: InfoUnitAgentAction
    target_info_unit_id: Text | None
    proposed: InfoUnitAgentDraft


ProposalValue = str | list[str] | None


class InfoUnitAgentChange(ContractModel):
    field: Literal[
        "type", "subtype", "title", "content", "evidence_ids", "speaker", "side",
        "actor", "target", "related_issue",
    ]
    old_value: ProposalValue
    new_value: ProposalValue


class InfoUnitAgentEvidence(ContractModel):
    quote_id: Text
    file_id: Text
    filename: Text
    text: Text
    role: Text
    context_quote_ids: list[Text]


class InfoUnitAgentProposalResponse(ContractModel):
    action: InfoUnitAgentAction
    target_info_unit_id: Text | None
    proposed: InfoUnitAgentDraft
    changes: list[InfoUnitAgentChange]
    unchanged_fields: list[Text]
    evidence: list[InfoUnitAgentEvidence]
    base_case_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class ConfirmInfoUnitAgentProposalInput(ContractModel):
    action: InfoUnitAgentAction
    target_info_unit_id: Text | None
    proposed: InfoUnitAgentDraft
    base_case_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]

    @model_validator(mode="after")
    def check_target(self) -> "ConfirmInfoUnitAgentProposalInput":
        if self.action == InfoUnitAgentAction.create and self.target_info_unit_id is not None:
            raise ValueError("create must not specify target_info_unit_id")
        if self.action == InfoUnitAgentAction.update and self.target_info_unit_id is None:
            raise ValueError("update requires target_info_unit_id")
        return self


class ExtractionRunInput(ContractModel):
    case_id: Text


class RelationRunInput(ContractModel):
    case_id: Text
