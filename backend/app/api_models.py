"""HTTP transport envelopes; business objects are defined only in schemas.py."""
from enum import Enum
from typing import Annotated, Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from app.schemas import (
    CaseData, Content, ContractModel, InfoUnitSubtype, InfoUnitType, Relation, RelationType, Text, Title,
)


class CaseResponse(ContractModel):
    case: CaseData | None


class CasesResponse(ContractModel):
    cases: list[CaseData]
    current_case_id: str | None


class AgentRunInput(ContractModel):
    goal: Annotated[str, Field(min_length=1, max_length=2000, pattern=r"\S")]
    selected_info_unit_ids: Annotated[list[Text], Field(max_length=5)] = Field(default_factory=list)
    current_issue: Text | None = None
    selected_reasoning_ids: Annotated[list[Text], Field(max_length=3)] = Field(default_factory=list)
    focused_object_id: Text | None = None

    @field_validator("selected_info_unit_ids")
    @classmethod
    def unique_ids(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("selected_info_unit_ids must be unique")
        return value


class UpdateCaseInput(ContractModel):
    case_title: Text


EvidenceIds = Annotated[list[Text], Field(min_length=1)]
WorkflowId = Annotated[str, Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")]


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
    clarify = "clarify"
    unsupported = "unsupported"


class InfoUnitMutationAction(str, Enum):
    create = "create"
    update = "update"


class InfoUnitAgentRequest(ContractModel):
    instruction: Annotated[str, Field(min_length=1, max_length=2000, pattern=r"\S")]
    selected_info_unit_ids: list[Text] = Field(default_factory=list)
    workflow_id: WorkflowId | None = None

    @field_validator("selected_info_unit_ids")
    @classmethod
    def unique_selected_ids(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("selected_info_unit_ids must be unique")
        return value


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
    proposed: InfoUnitAgentDraft | None
    message: Text | None

    @model_validator(mode="after")
    def check_action_payload(self) -> "ModelInfoUnitAgentProposal":
        if self.action == InfoUnitAgentAction.create:
            if self.target_info_unit_id is not None or self.proposed is None or self.message is not None:
                raise ValueError("create requires a proposal and no target or message")
        elif self.action == InfoUnitAgentAction.update:
            if self.target_info_unit_id is None or self.proposed is None or self.message is not None:
                raise ValueError("update requires a target and proposal, and no message")
        elif self.target_info_unit_id is not None or self.proposed is not None or self.message is None:
            raise ValueError("clarify/unsupported require only a nonempty message")
        return self


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
    proposed: InfoUnitAgentDraft | None
    changes: list[InfoUnitAgentChange]
    unchanged_fields: list[Text]
    evidence: list[InfoUnitAgentEvidence]
    base_case_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    message: Text | None


class ConfirmInfoUnitAgentProposalInput(ContractModel):
    action: InfoUnitMutationAction
    target_info_unit_id: Text | None
    proposed: InfoUnitAgentDraft
    base_case_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]

    @model_validator(mode="after")
    def check_target(self) -> "ConfirmInfoUnitAgentProposalInput":
        if self.action == InfoUnitMutationAction.create and self.target_info_unit_id is not None:
            raise ValueError("create must not specify target_info_unit_id")
        if self.action == InfoUnitMutationAction.update and self.target_info_unit_id is None:
            raise ValueError("update requires target_info_unit_id")
        return self


class ExtractionRunInput(ContractModel):
    case_id: Text
    workflow_id: WorkflowId | None = None


class RelationRunInput(ContractModel):
    case_id: Text
    selected_info_unit_ids: Annotated[list[Text], Field(min_length=2)] | None = None
    workflow_id: WorkflowId | None = None

    @field_validator("selected_info_unit_ids")
    @classmethod
    def unique_scope_ids(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and len(value) != len(set(value)):
            raise ValueError("selected_info_unit_ids must be unique")
        return value


class RelationRunResponse(ContractModel):
    status: Literal["empty", "success", "partial_success"]
    added_relations: list[Relation]
    discarded_count: int


class CreateRelationInput(ContractModel):
    source_id: Text
    target_id: Text
    relation_type: RelationType
    evidence_unit_ids: Annotated[list[Text], Field(min_length=1)]
    rationale: Text
    certainty: Literal["explicit", "inferred"]

    @field_validator("evidence_unit_ids")
    @classmethod
    def unique_evidence_ids(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("evidence_unit_ids must be unique")
        return value


class UpdateRelationInput(ContractModel):
    source_id: Text | None = None
    target_id: Text | None = None
    relation_type: RelationType | None = None
    evidence_unit_ids: Annotated[list[Text], Field(min_length=1)] | None = None
    rationale: Text | None = None
    certainty: Literal["explicit", "inferred"] | None = None

    @field_validator("evidence_unit_ids")
    @classmethod
    def unique_updated_evidence_ids(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and len(value) != len(set(value)):
            raise ValueError("evidence_unit_ids must be unique")
        return value

    @model_validator(mode="after")
    def require_changes(self) -> "UpdateRelationInput":
        if not self.model_fields_set:
            raise ValueError("At least one Relation field must be provided")
        if any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError("Relation fields cannot be null when provided")
        return self
