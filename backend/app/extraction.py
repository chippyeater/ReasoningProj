"""Strict model boundary and v0.3 materialization, without provider or storage writes."""
from __future__ import annotations

import json
from typing import Literal
from uuid import uuid4

from pydantic import ConfigDict, Field, ValidationError, field_validator, model_validator

from app.case_service import now_iso
from app.errors import AppError
from app.schemas import (
    CaseData, Content, ContractModel, ExtractionOutput, InfoUnit, InfoUnitSubtype,
    InfoUnitType, Text, Title, ValidationInput, check_info_classification,
)
from app.validation import locate_source_ref, validate_extraction


class ModelSourceQuote(ContractModel):
    """Only fields the model supplies; provenance and offsets are server owned."""
    model_config = ConfigDict(extra="forbid", strict=True)
    file_id: Text
    quote: Text = Field(description="一个连续、逐字一致且能唯一定位的原文片段；不得拼接或改写。")


class ModelInfoUnit(ContractModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    local_id: Text
    type: InfoUnitType
    subtype: InfoUnitSubtype | None
    title: Title
    content: Content = Field(description="保留必要条件、来源限定和不确定性的独立可核查信息。")
    source_quotes: list[ModelSourceQuote] = Field(min_length=1)
    speaker: Text | None
    side: Text | None
    actor_local_id: Text | None
    target_local_id: Text | None
    related_issue: Text | None

    @field_validator("title", "content")
    @classmethod
    def reject_surrounding_whitespace(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("text must not have leading or trailing whitespace")
        return value

    @model_validator(mode="after")
    def validate_business_fields(self) -> ModelInfoUnit:
        check_info_classification(self.type, self.subtype, allow_verified=False)
        if self.type in {InfoUnitType.state, InfoUnitType.position} and self.speaker is None:
            raise ValueError("state and position require a nonempty speaker")
        return self


class ModelEntity(ModelInfoUnit):
    type: Literal["entity"]
    subtype: Literal["person", "organization", "object", "location"]


class ModelEvent(ModelInfoUnit):
    type: Literal["event"]
    subtype: Literal["reported", "documented"] | None


class ModelState(ModelInfoUnit):
    type: Literal["state"]
    subtype: None


class ModelRule(ModelInfoUnit):
    type: Literal["rule"]
    subtype: None


class ModelPosition(ModelInfoUnit):
    type: Literal["position"]
    subtype: None


ModelInfoUnitUnion = ModelEntity | ModelEvent | ModelState | ModelRule | ModelPosition


class ModelExtraction(ContractModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    core_issue: Text | None
    issue_set: list[Text]
    info_units: list[ModelInfoUnitUnion]

    @model_validator(mode="after")
    def validate_batch(self) -> ModelExtraction:
        if len(set(self.issue_set)) != len(self.issue_set):
            raise ValueError("Duplicate issues")
        if self.core_issue is not None and self.core_issue not in self.issue_set:
            raise ValueError("core_issue must occur in issue_set")
        if any(u.related_issue is not None and u.related_issue not in self.issue_set for u in self.info_units):
            raise ValueError("related_issue must occur in issue_set")
        local_ids = [unit.local_id for unit in self.info_units]
        if len(local_ids) != len(set(local_ids)):
            raise ValueError("local_id must be unique within one model batch")
        lookup = {unit.local_id: unit for unit in self.info_units}
        for unit in self.info_units:
            for field in ("actor_local_id", "target_local_id"):
                target = getattr(unit, field)
                if target is not None and (target not in lookup or lookup[target].type != "entity"):
                    raise ValueError(f"{unit.local_id}.{field} must reference an entity local_id")
        return self


class ConsolidationDecision(ContractModel):
    """A concise editorial decision, not a new business entity or reasoning trace."""
    model_config = ConfigDict(extra="forbid", strict=True)
    draft_index: int = Field(ge=0)
    action: Literal["keep", "revise", "merge", "split", "remove"]
    finding: Text
    final_indices: list[int]


class ModelConsolidation(ContractModel):
    """Private review envelope; only its extraction is materialized as v0.3 data."""
    model_config = ConfigDict(extra="forbid", strict=True)
    decisions: list[ConsolidationDecision]
    coverage_findings: list[Text]
    added_final_indices: list[int]
    extraction: ModelExtraction

    def validate_plan(self, draft_count: int) -> None:
        indices = [d.draft_index for d in self.decisions]
        if sorted(indices) != list(range(draft_count)):
            raise ValueError("Review must address each draft index exactly once")
        final_count = len(self.extraction.info_units)
        covered: set[int] = set()
        for decision in self.decisions:
            targets = decision.final_indices
            if len(set(targets)) != len(targets) or any(i < 0 or i >= final_count for i in targets):
                raise ValueError("Invalid review target indices")
            expected = len(targets)
            if ((decision.action == "remove" and expected != 0)
                    or (decision.action == "split" and expected < 2)
                    or (decision.action in {"keep", "revise", "merge"} and expected != 1)):
                raise ValueError("Review action and target count disagree")
            covered.update(targets)
        additions = self.added_final_indices
        if len(set(additions)) != len(additions) or set(additions) != set(range(final_count)) - covered:
            raise ValueError("Every final unit must be accounted for by review decisions or explicit additions")


class ExtractionFailure(AppError):
    def __init__(self, code: str, message: str, *, errors: list[dict] | None = None, status_code: int = 422):
        super().__init__(code, message, status_code)
        self.errors = errors or []


def _unique_json_keys(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _invalid_json_constant(value: str) -> None:
    raise ValueError(f"Non-JSON constant: {value}")


def _parse_model(raw: str, model: type[ModelExtraction] | type[ModelConsolidation]):
    """No fence removal, JSON repair, type coercion, partial acceptance, or retry."""
    try:
        value = json.loads(raw, object_pairs_hook=_unique_json_keys, parse_constant=_invalid_json_constant)
    except (ValueError, TypeError) as exc:
        raise ExtractionFailure("extraction_json_invalid", "Model response is not strict JSON",
                                errors=[{"message": str(exc)}]) from exc
    try:
        return model.model_validate_json(json.dumps(value, ensure_ascii=False), strict=True)
    except ValidationError as exc:
        raise ExtractionFailure("extraction_schema_invalid", "Model result violates the extraction contract",
                                errors=exc.errors(include_url=False, include_input=False, include_context=False)) from exc


def parse_model_extraction(raw: str) -> ModelExtraction:
    return _parse_model(raw, ModelExtraction)


def parse_model_consolidation(raw: str, draft_count: int) -> ModelConsolidation:
    result = _parse_model(raw, ModelConsolidation)
    try:
        result.validate_plan(draft_count)
    except ValueError as exc:
        raise ExtractionFailure("extraction_schema_invalid", "Consolidation review is incomplete or inconsistent",
                                errors=[{"message": str(exc)}]) from exc
    return result


def _semantic_key(unit: InfoUnit) -> tuple:
    return (unit.type, unit.subtype, unit.content, unit.speaker, unit.side,
            unit.actor, unit.target, unit.related_issue)


def deduplicate_units(units: list[InfoUnit]) -> tuple[list[InfoUnit], list[dict]]:
    """Conservatively merge exact meanings and repair entity references."""
    entity_by_key: dict[tuple, InfoUnit] = {}
    entity_id_map: dict[str, str] = {}
    first_pass: list[InfoUnit] = []
    merges: list[dict] = []
    for unit in units:
        candidate = unit.model_copy(deep=True)
        if candidate.type != InfoUnitType.entity:
            first_pass.append(candidate)
            continue
        key = _semantic_key(candidate)
        if key not in entity_by_key:
            entity_by_key[key] = candidate
            first_pass.append(candidate)
        else:
            kept = entity_by_key[key]
            kept.source_refs.extend(candidate.source_refs)
            entity_id_map[candidate.id] = kept.id
            merges.append({"kept_id": kept.id, "merged_id": candidate.id})
    for unit in first_pass:
        if unit.actor in entity_id_map:
            unit.actor = entity_id_map[unit.actor]
        if unit.target in entity_id_map:
            unit.target = entity_id_map[unit.target]

    combined: dict[tuple, InfoUnit] = {}
    for unit in first_pass:
        key = _semantic_key(unit)
        if key not in combined:
            combined[key] = unit
        elif combined[key].id != unit.id:
            existing = combined[key]
            existing.source_refs.extend(unit.source_refs)
            merges.append({"kept_id": existing.id, "merged_id": unit.id})
    for unit in combined.values():
        refs = {(r.file_id, r.source_kind, r.quote, r.start, r.end): r for r in unit.source_refs}
        unit.source_refs = list(refs.values())
    return list(combined.values()), merges


def materialize_extraction(draft: ModelExtraction, case: CaseData) -> tuple[ExtractionOutput, list[dict]]:
    """Locate all quotes, allocate IDs, map local entity references, then validate atomically."""
    files = {f.file_id: f for f in case.files}
    formal_ids = {unit.local_id: f"info-{uuid4().hex}" for unit in draft.info_units}
    units: list[InfoUnit] = []
    errors: list[dict] = []
    created_at = now_iso()
    for index, unit in enumerate(draft.info_units):
        references = []
        for ref_index, ref in enumerate(unit.source_quotes):
            try:
                if ref.file_id not in files:
                    raise AppError("unknown_source_file", "Referenced file is not part of this case")
                references.append(locate_source_ref(case.case_id, files[ref.file_id], ref.quote))
            except AppError as exc:
                errors.append({"item_id": unit.local_id, "item_index": index,
                               "field": f"source_quotes.{ref_index}", "code": exc.code, "message": str(exc)})
        if references:
            payload = unit.model_dump(exclude={"local_id", "source_quotes", "actor_local_id", "target_local_id"})
            units.append(InfoUnit(
                **payload, id=formal_ids[unit.local_id], created_at=created_at, source_refs=references,
                actor=formal_ids[unit.actor_local_id] if unit.actor_local_id is not None else None,
                target=formal_ids[unit.target_local_id] if unit.target_local_id is not None else None,
            ))
    if errors:
        raise ExtractionFailure("extraction_source_invalid", "One or more source references could not be located",
                                errors=errors)
    units, merges = deduplicate_units(units)
    result = ExtractionOutput(core_issue=draft.core_issue, issue_set=draft.issue_set, info_units=units)
    validation = validate_extraction(case.case_id, ValidationInput(extraction=result, parsed_files=case.files))
    if not validation.valid:
        raise ExtractionFailure("extraction_source_invalid", "Source validation failed",
                                errors=[e.model_dump() for e in validation.errors])
    return result, merges
