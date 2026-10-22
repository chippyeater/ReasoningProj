"""Strict model boundary and v0.3 materialization, without provider or storage writes."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal
from uuid import uuid4

from pydantic import ConfigDict, Field, ValidationError, field_validator, model_validator

from app.case_service import now_iso
from app.errors import AppError
from app.quote_units import QuoteIndexEvent, quote_catalog, source_ref_from_quote_unit
from app.schemas import (
    CaseData, Content, ContractModel, ExtractionOutput, InfoUnit, InfoUnitSubtype,
    InfoUnitType, SourceRef, Text, Title, ValidationInput, check_info_classification,
)
from app.validation import validate_extraction
from app.validation_repair import (
    BATCH_ITEM_ID, issue, item_errors, locatable_items, pydantic_issues,
    require_exact_fields, strict_json_object,
)


class ModelInfoUnit(ContractModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    local_id: Text
    type: InfoUnitType
    subtype: InfoUnitSubtype | None
    title: Title
    content: Content = Field(description="保留必要条件、来源限定和不确定性的独立可核查信息。")
    evidence_ids: list[Text] = Field(
        min_length=1, description="从输入 Quote Units 中选择的一个或多个原文依据 ID。")
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
        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise ValueError("evidence_ids must be unique within one InfoUnit")
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


class ExtractionValidationRepair(ContractModel):
    """One-shot full replacements for failed candidates only."""
    model_config = ConfigDict(extra="forbid", strict=True)
    repairs: list[ModelInfoUnitUnion]
    unresolved_ids: list[Text]

    @field_validator("repairs", "unresolved_ids")
    @classmethod
    def unique_ids(cls, value):
        ids = [item.local_id for item in value] if value and hasattr(value[0], "local_id") else value
        if len(ids) != len(set(ids)):
            raise ValueError("repair IDs must be unique")
        return value

    @model_validator(mode="after")
    def disjoint_results(self) -> ExtractionValidationRepair:
        if {item.local_id for item in self.repairs} & set(self.unresolved_ids):
            raise ValueError("repairs and unresolved_ids must be disjoint")
        return self


class ExtractionFailure(AppError):
    def __init__(self, code: str, message: str, *, errors: list[dict] | None = None, status_code: int = 422):
        super().__init__(code, message, status_code)
        self.errors = errors or []
        self.diagnostics: dict = {}


def _unique_json_keys(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _invalid_json_constant(value: str) -> None:
    raise ValueError(f"Non-JSON constant: {value}")


def _parse_model(raw: str, model: type[ModelExtraction]):
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


_MODEL_BY_TYPE = {
    "entity": ModelEntity,
    "event": ModelEvent,
    "state": ModelState,
    "rule": ModelRule,
    "position": ModelPosition,
}


@dataclass
class ExtractionCandidateReport:
    core_issue: str | None
    issue_set: list[str]
    ordered_ids: list[str]
    raw_by_id: dict[str, dict[str, Any]]
    models_by_id: dict[str, ModelInfoUnitUnion]
    errors: list[dict[str, str]]
    source_diagnostics: list[dict[str, Any]]

    @property
    def failed_ids(self) -> set[str]:
        return {error["item_id"] for error in self.errors if error["item_id"] != BATCH_ITEM_ID}

    @property
    def accepted_ids(self) -> list[str]:
        failed = self.failed_ids
        return [item_id for item_id in self.ordered_ids
                if item_id in self.models_by_id and item_id not in failed]

    def accepted_draft(self) -> ModelExtraction:
        return ModelExtraction(
            core_issue=self.core_issue,
            issue_set=self.issue_set,
            info_units=[self.models_by_id[item_id] for item_id in self.accepted_ids],
        )

    def audit_payload(self) -> dict[str, Any]:
        grouped = item_errors(self.errors)
        return {
            "accepted_ids": self.accepted_ids,
            "failed_ids": [item_id for item_id in self.ordered_ids if item_id in self.failed_ids],
            "errors": self.errors,
            "candidates": [
                {
                    "local_id": item_id,
                    "status": "failed" if item_id in self.failed_ids else "accepted",
                    "errors": grouped.get(item_id, []),
                }
                for item_id in self.ordered_ids
            ],
            "source_diagnostics": self.source_diagnostics,
        }


def _header(value: dict[str, Any]) -> tuple[str | None, list[str]]:
    try:
        validated = ModelExtraction.model_validate({
            "core_issue": value.get("core_issue"),
            "issue_set": value.get("issue_set"),
            "info_units": [],
        }, strict=True)
    except ValidationError as exc:
        raise AppError(
            "extraction_schema_invalid", "Extraction batch header is invalid", 422,
            details={"errors": pydantic_issues(BATCH_ITEM_ID, exc)},
        ) from exc
    return validated.core_issue, list(validated.issue_set)


def parse_extraction_payload(raw: str) -> dict[str, Any]:
    value = strict_json_object(raw, code="extraction_json_invalid", label="Extraction output")
    require_exact_fields(
        value, {"core_issue", "issue_set", "info_units"},
        code="extraction_schema_invalid", label="Extraction output")
    _header(value)
    locatable_items(
        value["info_units"], list_field="info_units", id_field="local_id",
        code="extraction_schema_invalid", label="Extraction output")
    return value


def _validate_model_item(item_id: str, raw: dict[str, Any]) -> tuple[ModelInfoUnitUnion | None, list[dict[str, str]]]:
    model = _MODEL_BY_TYPE.get(raw.get("type"))
    if model is None:
        return None, [issue(item_id, "type", "invalid_info_type",
                            "type must be one of entity, event, state, rule, position")]
    try:
        return model.model_validate(raw, strict=True), []
    except ValidationError as exc:
        return None, pydantic_issues(item_id, exc)


def validate_extraction_candidates(payload: dict[str, Any], case: CaseData) -> ExtractionCandidateReport:
    """Validate locatable candidates independently, then propagate unsafe dependencies."""
    require_exact_fields(
        payload, {"core_issue", "issue_set", "info_units"},
        code="extraction_schema_invalid", label="Extraction output")
    core_issue, issue_set = _header(payload)
    located = locatable_items(
        payload["info_units"], list_field="info_units", id_field="local_id",
        code="extraction_schema_invalid", label="Extraction output")
    ordered_ids = [item_id for item_id, _ in located]
    raw_by_id = {item_id: raw for item_id, raw in located}
    models_by_id: dict[str, ModelInfoUnitUnion] = {}
    errors: list[dict[str, str]] = []
    for item_id, raw in located:
        model, model_errors = _validate_model_item(item_id, raw)
        errors.extend(model_errors)
        if model is not None:
            models_by_id[item_id] = model

    index_events: list[QuoteIndexEvent] = []
    catalog = quote_catalog(case, index_events)
    stale_rebuild = any(event.reason in {"text_hash_changed", "segmentation_version_changed"}
                        for event in index_events)
    source_diagnostics: list[dict[str, Any]] = []
    for item_id, unit in models_by_id.items():
        refs: list[SourceRef] = []
        source_errors: list[dict[str, str]] = []
        for evidence_index, evidence_id in enumerate(unit.evidence_ids):
            try:
                resolved = catalog.get(evidence_id)
                if resolved is None:
                    error_code = "stale_evidence_id" if stale_rebuild else "invalid_evidence_id"
                    raise AppError(error_code, f"Unknown current-case Quote Unit ID: {evidence_id}")
                quote_unit, file = resolved
                refs.append(source_ref_from_quote_unit(case.case_id, quote_unit, file))
            except AppError as exc:
                source_errors.append(issue(
                    item_id, f"evidence_ids.{evidence_index}", exc.code, str(exc)))
        errors.extend(source_errors)
        source_diagnostics.append({
            "local_id": item_id,
            "evidence_ids": list(unit.evidence_ids),
            "source_refs": [ref.model_dump(mode="json") for ref in refs],
            "support_review": assess_evidence_support(unit.content, refs) if refs else {
                "deterministic_status": "needs_review",
                "semantic_support": "needs_review",
                "findings": [{"code": "no_materialized_evidence",
                              "message": "No evidence could be materialized"}],
            },
        })

    for item_id, unit in models_by_id.items():
        if unit.related_issue is not None and unit.related_issue not in issue_set:
            errors.append(issue(item_id, "related_issue", "unknown_related_issue",
                                "related_issue must occur in issue_set"))
        for field in ("actor_local_id", "target_local_id"):
            target_id = getattr(unit, field)
            if target_id is None:
                continue
            target = models_by_id.get(target_id)
            if target is None:
                errors.append(issue(item_id, field, "invalid_info_unit_reference",
                                    f"{field} must reference a valid entity local_id"))
            elif target.type != InfoUnitType.entity:
                errors.append(issue(item_id, field, "invalid_entity_reference",
                                    f"{field} must reference an entity local_id"))

    failed = {error["item_id"] for error in errors}
    changed = True
    while changed:
        changed = False
        for item_id, unit in models_by_id.items():
            if item_id in failed:
                continue
            for field in ("actor_local_id", "target_local_id"):
                target_id = getattr(unit, field)
                if target_id is not None and target_id in failed:
                    errors.append(issue(item_id, field, "dependency_invalid",
                                        f"Referenced entity candidate {target_id} is not safely acceptable"))
                    failed.add(item_id)
                    changed = True
                    break
    return ExtractionCandidateReport(
        core_issue=core_issue,
        issue_set=issue_set,
        ordered_ids=ordered_ids,
        raw_by_id=raw_by_id,
        models_by_id=models_by_id,
        errors=errors,
        source_diagnostics=source_diagnostics,
    )


def _text_signatures(value: str) -> set[str]:
    normalized = "".join(character.lower() for character in value if character.isalnum())
    if len(normalized) < 2:
        return {normalized} if normalized else set()
    return {normalized[index:index + 2] for index in range(len(normalized) - 1)}


def extraction_repair_evidence(case: CaseData, failed_items: list[dict[str, Any]],
                               *, limit_per_item: int = 10) -> list[dict[str, Any]]:
    """Select a bounded, deterministic evidence neighborhood instead of resending all files."""
    catalog = quote_catalog(case)
    selected_ids: set[str] = set()
    for raw in failed_items:
        query = _text_signatures(" ".join(str(raw.get(field) or "")
                                           for field in ("title", "content", "speaker")))
        referenced = raw.get("evidence_ids") if isinstance(raw.get("evidence_ids"), list) else []
        selected_ids.update(item for item in referenced if isinstance(item, str) and item in catalog)
        ranked: list[tuple[int, int, str]] = []
        for quote_id, (unit, _) in catalog.items():
            if unit.role == "group_boundary":
                continue
            score = len(query & _text_signatures(unit.text))
            ranked.append((score, -unit.start, quote_id))
        ranked.sort(reverse=True)
        selected_ids.update(quote_id for score, _, quote_id in ranked[:limit_per_item] if score > 0)
    for quote_id in list(selected_ids):
        unit, _ = catalog[quote_id]
        selected_ids.update(context_id for context_id in unit.context_quote_ids if context_id in catalog)
    return [
        {
            "quote_id": quote_id,
            "file_id": unit.file_id,
            "filename": file.filename,
            "text": unit.text,
            "role": unit.role,
            "context_quote_ids": list(unit.context_quote_ids),
        }
        for quote_id, (unit, file) in catalog.items() if quote_id in selected_ids
    ]


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


_FULL_DATE = re.compile(r"(?<!\d)(\d{4})\s*(?:年|[-/.])\s*(\d{1,2})\s*(?:月|[-/.])\s*(\d{1,2})\s*日?")
_MONTH_DAY = re.compile(r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]")
_AMOUNT = re.compile(r"(?<!\d)(\d[\d,，]*)\s*元")


def _date_signatures(value: str) -> set[tuple[int | None, int, int]]:
    signatures = {(int(year), int(month), int(day))
                  for year, month, day in _FULL_DATE.findall(value)}
    signatures.update((None, int(month), int(day)) for month, day in _MONTH_DAY.findall(value))
    return signatures


def _date_supported(candidate: tuple[int | None, int, int], evidence: set[tuple[int | None, int, int]]) -> bool:
    year, month, day = candidate
    return any(month == other_month and day == other_day
               and (year is None or other_year is None or year == other_year)
               for other_year, other_month, other_day in evidence)


def assess_evidence_support(content: str, references: list[SourceRef]) -> dict:
    """Explain conservative amount/date checks without claiming semantic proof."""
    evidence_text = "\n".join(ref.quote for ref in references)
    content_amounts = {int(raw.replace(",", "").replace("，", "")) for raw in _AMOUNT.findall(content)}
    evidence_amounts = {int(raw.replace(",", "").replace("，", "")) for raw in _AMOUNT.findall(evidence_text)}
    content_dates = _date_signatures(content)
    evidence_dates = _date_signatures(evidence_text)
    findings: list[dict] = []
    for amount in sorted(content_amounts - evidence_amounts):
        findings.append({"code": "amount_not_in_selected_evidence", "value": amount,
                         "message": f"Content amount {amount}元 is not present in selected Quote Units"})
    for date in sorted(content_dates, key=lambda item: (item[0] or 0, item[1], item[2])):
        if not _date_supported(date, evidence_dates):
            year, month, day = date
            display = f"{year}年{month}月{day}日" if year is not None else f"{month}月{day}日"
            findings.append({"code": "date_not_in_selected_evidence", "value": display,
                             "message": f"Content date {display} is not present in selected Quote Units"})
    return {
        "deterministic_status": "needs_review" if findings else "passed",
        "semantic_support": "needs_review",
        "findings": findings,
        "note": "Only explicit amount/date consistency is checked; natural-language support is not proven.",
    }


def materialize_extraction(draft: ModelExtraction, case: CaseData,
                           diagnostics: dict | None = None) -> tuple[ExtractionOutput, list[dict]]:
    """Resolve Quote Unit IDs, allocate formal IDs, and validate atomically."""
    index_events: list[QuoteIndexEvent] = []
    catalog = quote_catalog(case, index_events)
    stale_rebuild = any(event.reason in {"text_hash_changed", "segmentation_version_changed"}
                        for event in index_events)
    formal_ids = {unit.local_id: f"info-{uuid4().hex}" for unit in draft.info_units}
    units: list[InfoUnit] = []
    errors: list[dict] = []
    candidates: list[dict] = []
    created_at = now_iso()
    for index, unit in enumerate(draft.info_units):
        references = []
        unit_errors: list[dict] = []
        for evidence_index, evidence_id in enumerate(unit.evidence_ids):
            try:
                resolved = catalog.get(evidence_id)
                if resolved is None:
                    code = "stale_evidence_id" if stale_rebuild else "invalid_evidence_id"
                    raise AppError(code, f"Unknown current-case Quote Unit ID: {evidence_id}")
                quote_unit, file = resolved
                references.append(source_ref_from_quote_unit(case.case_id, quote_unit, file))
            except AppError as exc:
                error = {"item_id": unit.local_id, "item_index": index,
                         "field": f"evidence_ids.{evidence_index}", "candidate_id": evidence_id,
                         "code": exc.code, "message": str(exc)}
                unit_errors.append(error)
                errors.append(error)
        support = assess_evidence_support(unit.content, references) if references else {
            "deterministic_status": "needs_review", "semantic_support": "needs_review",
            "findings": [{"code": "no_materialized_evidence", "message": "No evidence could be materialized"}],
            "note": "Only explicit amount/date consistency is checked; natural-language support is not proven.",
        }
        candidates.append({
            "local_id": unit.local_id, "status": "failed" if unit_errors else "passed",
            "evidence_ids": list(unit.evidence_ids),
            "source_refs": [ref.model_dump(mode="json") for ref in references],
            "support_review": support, "errors": unit_errors,
        })
        if not unit_errors:
            payload = unit.model_dump(exclude={"local_id", "evidence_ids", "actor_local_id", "target_local_id"})
            units.append(InfoUnit(
                **payload, id=formal_ids[unit.local_id], created_at=created_at, source_refs=references,
                actor=formal_ids[unit.actor_local_id] if unit.actor_local_id is not None else None,
                target=formal_ids[unit.target_local_id] if unit.target_local_id is not None else None,
            ))
    detail = {
        "quote_indexes": [event.model_dump(mode="json") for event in index_events],
        "candidates": candidates,
        "passed_local_ids": [item["local_id"] for item in candidates if item["status"] == "passed"],
        "failed_local_ids": [item["local_id"] for item in candidates if item["status"] == "failed"],
    }
    if diagnostics is not None:
        diagnostics.clear()
        diagnostics.update(detail)
    if errors:
        failure = ExtractionFailure("extraction_source_invalid",
                                    "One or more Quote Unit references could not be materialized", errors=errors)
        failure.diagnostics = detail
        raise failure
    units, merges = deduplicate_units(units)
    result = ExtractionOutput(core_issue=draft.core_issue, issue_set=draft.issue_set, info_units=units)
    validation = validate_extraction(case.case_id, ValidationInput(extraction=result, parsed_files=case.files))
    if not validation.valid:
        failure = ExtractionFailure("extraction_source_invalid", "Source validation failed",
                                    errors=[e.model_dump() for e in validation.errors])
        failure.diagnostics = detail
        raise failure
    return result, merges
