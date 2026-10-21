"""Frozen v0.2 extraction/relation contract for reproducible Gold v1.7 checks."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceRef(FrozenModel):
    file_id: str
    source_kind: Literal["document", "communication", "testimony", "record", "other"]
    quote: str
    start: int
    end: int


class InfoUnit(FrozenModel):
    id: str
    type: Literal["entity", "event", "state", "statement"]
    subtype: Literal["person", "organization", "object", "location", "action", "occurrence", "transfer",
                     "communication", "temporal", "quantitative", "descriptive", "fact_assertion", "assessment", "provision"]
    title: str
    summary: str
    content: str
    source_refs: list[SourceRef]
    speaker: str | None
    side: str | None
    actor: str | None
    target: str | None
    stance_target: str | None
    created_at: str


class ExtractionOutput(FrozenModel):
    core_issue: str | None
    issue_set: list[str]
    info_units: list[InfoUnit]

    @model_validator(mode="after")
    def check_issues(self):
        if self.core_issue is not None and self.core_issue not in self.issue_set:
            raise ValueError("core issue is not registered")
        if any(unit.stance_target is not None and unit.stance_target not in self.issue_set for unit in self.info_units):
            raise ValueError("stance target is not registered")
        return self


class Relation(FrozenModel):
    id: str
    source_id: str
    target_id: str
    relation_type: Literal["actor_of", "target_of", "about", "support", "contradict", "occur_at"]
    evidence_unit_ids: list[str]
    rationale: str
    certainty: Literal["explicit", "inferred"]


def check_relations(units: list[InfoUnit], relations: list[Relation]) -> None:
    identifiers = {unit.id for unit in units}
    for relation in relations:
        if relation.source_id == relation.target_id:
            raise ValueError("self relation")
        if not {relation.source_id, relation.target_id, *relation.evidence_unit_ids} <= identifiers:
            raise ValueError("unknown legacy relation reference")
