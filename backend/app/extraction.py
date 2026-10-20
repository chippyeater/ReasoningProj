"""Strict model boundary and v0.2 materialization, without provider or storage writes."""
from __future__ import annotations

import json
from uuid import uuid4
from typing import Literal

from pydantic import ConfigDict, Field, ValidationError, model_validator

from app.case_service import now_iso
from app.errors import AppError
from app.schemas import (
    CaseData, ContractModel, ExtractionOutput, InfoUnit, InfoUnitSubtype, InfoUnitType,
    Text, ValidationInput, check_info_classification,
)
from app.validation import locate_source_ref, validate_extraction


class ModelCitation(ContractModel):
    """Only fields the model supplies; provenance and offsets are server owned."""
    model_config = ConfigDict(extra="forbid", strict=True)
    file_id: Text
    quote: Text = Field(description="一个连续、逐字一致且能唯一定位的原文片段；不得省略中间文字、改空格或拼接不同段落。")


class ModelInfoUnit(ContractModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    type: InfoUnitType
    subtype: InfoUnitSubtype
    title: Text
    summary: Text
    content: Text = Field(description="一个可独立引用的主命题，保留时间、条件、不确定性和陈述归属。禁止整份材料摘要或把事实、评价、规则混在一条中。")
    source_refs: list[ModelCitation] = Field(min_length=1)
    speaker: Text | None
    side: Text | None
    actor: Text | None
    target: Text | None
    stance_target: Text | None

    @model_validator(mode="after")
    def validate_classification(self) -> ModelInfoUnit:
        check_info_classification(InfoUnitType(self.type), InfoUnitSubtype(self.subtype))
        return self


class ModelEntity(ModelInfoUnit):
    type: Literal["entity"]
    subtype: Literal["person", "organization", "object", "location"]


class ModelEvent(ModelInfoUnit):
    type: Literal["event"]
    subtype: Literal["action", "occurrence", "transfer", "communication"]


class ModelState(ModelInfoUnit):
    type: Literal["state"]
    subtype: Literal["temporal", "quantitative", "descriptive"]


class ModelStatement(ModelInfoUnit):
    type: Literal["statement"]
    subtype: Literal["fact_assertion", "assessment", "provision"]


class ModelExtraction(ContractModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    core_issue: Text | None
    issue_set: list[Text]
    info_units: list[ModelEntity | ModelEvent | ModelState | ModelStatement]

    @model_validator(mode="after")
    def validate_issues(self) -> ModelExtraction:
        if len(set(self.issue_set)) != len(self.issue_set):
            raise ValueError("Duplicate issues")
        if self.core_issue is not None and self.core_issue not in self.issue_set:
            raise ValueError("core_issue must occur in issue_set")
        if any(u.stance_target is not None and u.stance_target not in self.issue_set for u in self.info_units):
            raise ValueError("stance_target must occur in issue_set")
        return self


class ConsolidationDecision(ContractModel):
    """A concise editorial decision, not a new business entity or a reasoning trace."""
    model_config = ConfigDict(extra="forbid", strict=True)
    draft_index: int = Field(ge=0)
    action: Literal["keep", "revise", "merge", "split", "remove"]
    finding: Text
    final_indices: list[int]


class ModelConsolidation(ContractModel):
    """Private review envelope; only extraction is materialized as v0.2 data."""
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
    # Conservative equality only. Do not conflate different speakers, times,
    # qualifiers, facts, classifications or stances on a similarity threshold.
    return (unit.type, unit.subtype, unit.content, unit.speaker, unit.side,
            unit.actor, unit.target, unit.stance_target)


def deduplicate_units(units: list[InfoUnit]) -> tuple[list[InfoUnit], list[dict]]:
    """Merge identical meanings expressed by the same qualified semantic fields.

    Cross-file semantic consolidation is first performed jointly by the model.
    This deterministic pass only merges exact content matches and unions sources.
    Different content remains available for review instead of being guessed away.
    """
    combined: dict[tuple, InfoUnit] = {}
    merges = []
    for unit in units:
        key = _semantic_key(unit)
        if key not in combined:
            combined[key] = unit.model_copy(deep=True)
        else:
            existing = combined[key]
            existing.source_refs.extend(unit.source_refs)
            merges.append({"kept_id": existing.id, "merged_id": unit.id})
    for unit in combined.values():
        refs = {(r.file_id, r.source_kind, r.quote, r.start, r.end): r for r in unit.source_refs}
        unit.source_refs = list(refs.values())
    return list(combined.values()), merges


def materialize_extraction(draft: ModelExtraction, case: CaseData) -> tuple[ExtractionOutput, list[dict]]:
    files = {f.file_id: f for f in case.files}
    units = []
    errors = []
    created_at = now_iso()
    for index, unit in enumerate(draft.info_units):
        references = []
        for ref_index, ref in enumerate(unit.source_refs):
            try:
                if ref.file_id not in files:
                    raise AppError("unknown_source_file", "Referenced file is not part of this case")
                references.append(locate_source_ref(case.case_id, files[ref.file_id], ref.quote))
            except AppError as exc:
                errors.append({"item_index": index, "field": f"source_refs.{ref_index}",
                               "code": exc.code, "message": str(exc)})
        if references:
            units.append(InfoUnit(**unit.model_dump(exclude={"source_refs"}),
                                  id=f"info-{uuid4().hex}", created_at=created_at, source_refs=references))
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
