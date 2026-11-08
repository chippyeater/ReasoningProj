"""Issue reasoning objects, deliberately separate from InfoUnit/Relation Schema."""
from typing import Annotated, Literal

from pydantic import ConfigDict, Field, TypeAdapter, model_validator

from app.schemas import ContractModel, Point, Text, SourceRef

ReasoningText = Annotated[str, Field(min_length=1, max_length=600, pattern=r"\S")]
Judgment = Literal["unverified", "verified", "refuted"]
LinkMeaning = Literal["support", "oppose", "related"]


class Hypothesis(ContractModel):
    id: Text
    content: ReasoningText
    judgment: Judgment = "unverified"
    work_status: Literal["active", "deprecated"] = "active"
    revision: int = 1
    updated_at: Text
    last_edited_by: Literal["user", "agent_accepted"] = "user"
    proposal_key: str | None = None


class EvidenceGap(ContractModel):
    id: Text
    content: ReasoningText
    hypothesis_ids: list[Text]
    basis_info_unit_ids: list[Text] = Field(default_factory=list)
    addressing_info_unit_ids: list[Text] = Field(default_factory=list)
    status: Literal["open", "resolved"] = "open"
    revision: int = 1
    updated_at: Text
    last_edited_by: Literal["user", "agent_accepted"] = "user"
    proposal_key: str | None = None


class EvidenceLink(ContractModel):
    id: Text
    hypothesis_id: Text
    info_unit_id: Text
    meaning: LinkMeaning = "related"
    rationale: ReasoningText
    origin: Literal["user", "agent"] = "user"
    last_edited_by: Literal["user", "agent_accepted"] = "user"
    proposal_key: str | None = None


class HypothesisEdit(ContractModel):
    id: Text | None = None
    content: ReasoningText
    judgment: Judgment = "unverified"
    work_status: Literal["active", "deprecated"] = "active"


class GapEdit(ContractModel):
    id: Text | None = None
    content: ReasoningText
    hypothesis_ids: list[Text]
    basis_info_unit_ids: list[Text] = Field(default_factory=list)
    addressing_info_unit_ids: list[Text] = Field(default_factory=list)
    remove_addressing_info_unit_ids: list[Text] = Field(default_factory=list)
    status: Literal["open", "resolved"] = "open"


class LinkEdit(ContractModel):
    id: Text | None = None
    hypothesis_id: Text
    info_unit_id: Text
    meaning: LinkMeaning = "related"
    rationale: ReasoningText


class ReasoningEdit(ContractModel):
    issue: Text
    hypothesis: HypothesisEdit | None = None
    gap: GapEdit | None = None
    link: LinkEdit | None = None
    unlink_id: Text | None = None
    delete_hypothesis_id: Text | None = None
    delete_gap_id: Text | None = None
    position: Point | None = None

    @model_validator(mode="after")
    def single_operation(self):
        if sum(value is not None for value in (self.hypothesis, self.gap, self.link, self.unlink_id,
                                               self.delete_hypothesis_id, self.delete_gap_id)) != 1:
            raise ValueError("Exactly one reasoning edit is required")
        return self


class InvestigationSourceRef(ContractModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    info_unit_id: Text
    source_ref_index: Annotated[int, Field(ge=0)]


class ReasoningChange(ContractModel):
    collection: Literal["hypotheses", "gaps", "links"]
    object_id: Text
    before: Hypothesis | EvidenceGap | EvidenceLink | None
    after: Hypothesis | EvidenceGap | EvidenceLink | None
    proposal_keys: list[str] = Field(default_factory=list)


class ProposalBase(ContractModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    proposal_id: Annotated[str, Field(pattern=r"^proposal-[a-f0-9]{32}$")] | None = None
    content: ReasoningText | None
    hypothesis_ids: Annotated[list[Text], Field(max_length=5)]
    # Relative only to the immutable proposals array in the enclosing run.
    hypothesis_proposal_indices: Annotated[list[Annotated[int, Field(ge=0, lt=6)]], Field(max_length=5)] = Field(default_factory=list)
    hypothesis_proposal_ids: Annotated[list[Annotated[str, Field(pattern=r"^proposal-[a-f0-9]{32}$")]], Field(max_length=5)] = Field(default_factory=list)
    meaning: LinkMeaning | None
    judgment: Judgment | None
    rationale: ReasoningText
    # Server-generated audit of the operation actually applied to this run.
    changes: list[ReasoningChange] = Field(default_factory=list)

    @model_validator(mode="after")
    def shape(self):
        if self.kind in {"evidence_link", "hypothesis_status", "evidence_gap_update"} and not proposal_material_ids(self):
            raise ValueError("Grounded proposals require obtained InfoUnit IDs")
        material_field = ("basis_info_unit_ids" if self.kind == "evidence_gap" else
                          "addressing_info_unit_ids" if self.kind == "evidence_gap_update" else "info_unit_ids")
        for field in ("hypothesis_ids", material_field):
            if len(getattr(self, field)) != len(set(getattr(self, field))):
                raise ValueError(f"Duplicate {field}")
        if len(self.hypothesis_proposal_indices) != len(set(self.hypothesis_proposal_indices)):
            raise ValueError("Duplicate hypothesis_proposal_indices")
        if len(self.hypothesis_proposal_ids) != len(set(self.hypothesis_proposal_ids)):
            raise ValueError("Duplicate hypothesis_proposal_ids")
        if self.hypothesis_proposal_indices and self.hypothesis_proposal_ids:
            raise ValueError("Do not mix legacy indices with hypothesis_proposal_ids")
        if self.hypothesis_proposal_indices and self.kind != "evidence_gap":
            raise ValueError("Legacy hypothesis_proposal_indices is only allowed for evidence_gap")
        if self.hypothesis_proposal_ids and self.kind not in {"evidence_gap", "evidence_link"}:
            raise ValueError("hypothesis_proposal_ids is only allowed for evidence_gap and evidence_link")
        if self.kind == "hypothesis":
            valid = self.content is not None and not self.hypothesis_ids and self.meaning is None and self.judgment is None
        elif self.kind == "evidence_gap":
            if not (self.hypothesis_ids or self.hypothesis_proposal_indices or self.hypothesis_proposal_ids):
                raise ValueError("Proposal fields do not match kind: evidence_gap requires hypothesis_ids, hypothesis_proposal_ids or legacy hypothesis_proposal_indices")
            if len(self.hypothesis_ids) + len(self.hypothesis_proposal_indices) + len(self.hypothesis_proposal_ids) > 5:
                raise ValueError("hypothesis_ids and hypothesis_proposal_indices must total at most 5 targets")
            valid = self.content is not None and self.meaning is None and self.judgment is None
        elif self.kind == "evidence_link":
            valid = (self.content is None and len(self.hypothesis_ids) + len(self.hypothesis_proposal_ids) == 1
                     and len(self.info_unit_ids) == 1 and self.meaning is not None and self.judgment is None)
        elif self.kind == "evidence_gap_update":
            valid = self.content is None and not self.hypothesis_ids and self.meaning is None and self.judgment is None
        else:
            valid = self.content is None and len(self.hypothesis_ids) == 1 and self.meaning is None and self.judgment is not None
        if not valid:
            raise ValueError("Proposal fields do not match kind: check content, hypothesis_ids / hypothesis_proposal_indices, meaning and judgment")
        return self


class MaterialProposal(ProposalBase):
    kind: Literal["hypothesis", "evidence_link", "hypothesis_status"]
    info_unit_ids: Annotated[list[Text], Field(max_length=5)]


class EvidenceGapProposal(ProposalBase):
    kind: Literal["evidence_gap"]
    basis_info_unit_ids: Annotated[list[Text], Field(max_length=5)]


class EvidenceGapUpdateProposal(ProposalBase):
    kind: Literal["evidence_gap_update"]
    gap_id: Text
    addressing_info_unit_ids: Annotated[list[Text], Field(min_length=1, max_length=5)]
    status: Literal["open", "resolved"]


ReasoningProposal = Annotated[MaterialProposal | EvidenceGapProposal | EvidenceGapUpdateProposal, Field(discriminator="kind")]
PROPOSAL_ADAPTER = TypeAdapter(ReasoningProposal)


def proposal_material_ids(proposal: ReasoningProposal) -> list[str]:
    """References used for citation validation and adoption staleness checks."""
    if proposal.kind == "evidence_gap":
        return proposal.basis_info_unit_ids
    if proposal.kind == "evidence_gap_update":
        return proposal.addressing_info_unit_ids
    return proposal.info_unit_ids


class ReasoningRunEvidence(ContractModel):
    info_unit_id: Text
    source_ref_index: int
    source_ref: SourceRef


class ReasoningRun(ContractModel):
    status: Literal["applied", "undone"] = "applied"
    changes: list[ReasoningChange]
    proposals: list[ReasoningProposal] = Field(default_factory=list)
    evidence: list[ReasoningRunEvidence] = Field(default_factory=list)


class ReasoningState(ContractModel):
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    gaps: list[EvidenceGap] = Field(default_factory=list)
    links: list[EvidenceLink] = Field(default_factory=list)
    accepted_proposals: dict[str, str] = Field(default_factory=dict)
    investigation_runs: dict[str, ReasoningRun] = Field(default_factory=dict)

    @model_validator(mode="after")
    def references(self):
        ids = [obj.id for obj in [*self.hypotheses, *self.gaps, *self.links]]
        if len(ids) != len(set(ids)):
            raise ValueError("Reasoning IDs must be unique")
        hypotheses = {h.id for h in self.hypotheses}
        pairs = [(link.hypothesis_id, link.info_unit_id) for link in self.links]
        if len(pairs) != len(set(pairs)):
            raise ValueError("Only one evidence link per InfoUnit and Hypothesis")
        for gap in self.gaps:
            if not gap.hypothesis_ids or not set(gap.hypothesis_ids) <= hypotheses:
                raise ValueError("Gap must refer to current Issue hypotheses")
            if any(len(ids) != len(set(ids)) for ids in (
                    gap.hypothesis_ids, gap.basis_info_unit_ids, gap.addressing_info_unit_ids)):
                raise ValueError("Duplicate gap references")
        if any(link.hypothesis_id not in hypotheses for link in self.links):
            raise ValueError("Unknown link Hypothesis")
        return self


class InvestigationRunRef(ContractModel):
    issue: Text
    run_id: Annotated[str, Field(pattern=r"^investigation-[a-f0-9]{32}$")]


class InvestigationRunEdit(InvestigationRunRef):
    restore: bool = False
