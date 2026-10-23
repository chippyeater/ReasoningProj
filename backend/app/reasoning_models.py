"""Issue reasoning objects, deliberately separate from InfoUnit/Relation Schema."""
from typing import Annotated, Literal

from pydantic import Field, model_validator

from app.schemas import ContractModel, Point, Text

ReasoningText = Annotated[str, Field(min_length=1, max_length=600, pattern=r"\S")]
Judgment = Literal["unverified", "verified", "refuted"]
LinkMeaning = Literal["support", "oppose", "verify"]


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
    info_unit_ids: list[Text] = Field(default_factory=list)
    status: Literal["open", "resolved"] = "open"
    revision: int = 1
    updated_at: Text
    last_edited_by: Literal["user", "agent_accepted"] = "user"
    proposal_key: str | None = None


class EvidenceLink(ContractModel):
    id: Text
    hypothesis_id: Text
    info_unit_id: Text
    meaning: LinkMeaning
    rationale: ReasoningText
    origin: Literal["user", "agent"] = "user"
    last_edited_by: Literal["user", "agent_accepted"] = "user"
    proposal_key: str | None = None


class ReasoningState(ContractModel):
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    gaps: list[EvidenceGap] = Field(default_factory=list)
    links: list[EvidenceLink] = Field(default_factory=list)
    accepted_proposals: dict[str, str] = Field(default_factory=dict)

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
            if len(gap.hypothesis_ids) != len(set(gap.hypothesis_ids)) or len(gap.info_unit_ids) != len(set(gap.info_unit_ids)):
                raise ValueError("Duplicate gap references")
        if any(link.hypothesis_id not in hypotheses for link in self.links):
            raise ValueError("Unknown link Hypothesis")
        return self


class HypothesisEdit(ContractModel):
    id: Text | None = None
    content: ReasoningText
    judgment: Judgment = "unverified"
    work_status: Literal["active", "deprecated"] = "active"


class GapEdit(ContractModel):
    id: Text | None = None
    content: ReasoningText
    hypothesis_ids: list[Text]
    info_unit_ids: list[Text] = Field(default_factory=list)
    status: Literal["open", "resolved"] = "open"


class LinkEdit(ContractModel):
    id: Text | None = None
    hypothesis_id: Text
    info_unit_id: Text
    meaning: LinkMeaning
    rationale: ReasoningText


class ReasoningEdit(ContractModel):
    issue: Text
    hypothesis: HypothesisEdit | None = None
    gap: GapEdit | None = None
    link: LinkEdit | None = None
    unlink_id: Text | None = None
    position: Point | None = None

    @model_validator(mode="after")
    def single_operation(self):
        if sum(value is not None for value in (self.hypothesis, self.gap, self.link, self.unlink_id)) != 1:
            raise ValueError("Exactly one reasoning edit is required")
        return self
