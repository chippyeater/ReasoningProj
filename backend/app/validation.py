"""Validate v0.3 results against fixed cached text and confirmed provenance."""
from __future__ import annotations

from app.case_store import read_parsed_document
from app.errors import AppError
from app.schemas import (
    CaseFile, ExtractionOutput, InfoUnitType, Relation, RelationInput, RelationOutput, RelationType,
    SourceRef, ValidationErrorItem, ValidationInput, ValidationResult, check_relations,
)
from app.validation_repair import issue


def locate_source_ref(case_id: str, file: CaseFile, quote: str) -> SourceRef:
    """Compute offsets; refuse an absent or ambiguous quote, including overlaps."""
    if not quote.strip():
        raise AppError("empty_quote", "Source quote must be nonempty")
    text = read_parsed_document(case_id, file).text
    start = text.find(quote)
    if start < 0:
        raise AppError("quote_not_found", f"{file.file_id}: quote does not occur in cached TXT")
    if text.find(quote, start + 1) >= 0:
        raise AppError("ambiguous_quote", f"{file.file_id}: quote has multiple occurrences")
    return SourceRef(file_id=file.file_id, source_kind=file.source_kind,
                     quote=quote, start=start, end=start + len(quote))


def validate_extraction(case_id: str, value: ValidationInput) -> ValidationResult[ExtractionOutput]:
    """Validate against persisted upload metadata and TXT; reject the whole invalid batch."""
    files = {f.file_id: f for f in value.parsed_files}
    errors: list[ValidationErrorItem] = []
    documents: dict[str, str] = {}
    for unit in value.extraction.info_units:
        for index, ref in enumerate(unit.source_refs):
            field = f"source_refs.{index}"
            try:
                file = files.get(ref.file_id)
                if file is None:
                    raise AppError("unknown_source_file", "Source file is not part of this case")
                if ref.source_kind != file.source_kind:
                    raise AppError("source_kind_mismatch", "Source kind differs from confirmed file metadata")
                if ref.file_id not in documents:
                    documents[ref.file_id] = read_parsed_document(case_id, file).text
                text = documents[ref.file_id]
                if ref.end > len(text) or text[ref.start:ref.end] != ref.quote:
                    raise AppError("quote_mismatch", "Quote differs from the cached TXT slice")
            except (AppError, ValueError) as exc:
                errors.append(ValidationErrorItem(item_id=unit.id, field=field, message=str(exc)))
    return ValidationResult[ExtractionOutput](
        valid=not errors, data=value.extraction if not errors else None, errors=errors)


def validate_relations(value: RelationOutput, context: RelationInput) -> ValidationResult[RelationOutput]:
    _, detailed_errors = validate_relation_candidates(value.relations, context)
    if detailed_errors:
        return ValidationResult[RelationOutput](valid=False, data=None, errors=[
            ValidationErrorItem(
                item_id=error["item_id"], field=error["field_path"], message=error["message"])
            for error in detailed_errors
        ])
    return ValidationResult[RelationOutput](valid=True, data=value, errors=[])


def relation_key(relation: Relation) -> tuple[str, str, RelationType]:
    source, target = relation.source_id, relation.target_id
    if relation.relation_type == RelationType.contradict and target < source:
        source, target = target, source
    return source, target, relation.relation_type


def validate_relation_candidates(
        relations: list[Relation], context: RelationInput,
        existing_relations: list[Relation] | None = None,
        ) -> tuple[list[Relation], list[dict[str, str]]]:
    """Validate each Relation independently while preserving the first legal equivalent."""
    lookup = {unit.id: unit for unit in context.info_units}
    existing_keys = {relation_key(relation) for relation in (existing_relations or [])}
    seen_keys: set[tuple[str, str, RelationType]] = set()
    accepted: list[Relation] = []
    errors: list[dict[str, str]] = []
    for relation in relations:
        item_errors: list[dict[str, str]] = []
        for field in ("source_id", "target_id"):
            value = getattr(relation, field)
            if value not in lookup:
                item_errors.append(issue(
                    relation.id, field, "relation_scope_violation",
                    f"{field} must reference an InfoUnit in the current Relation scope"))
        for index, evidence_id in enumerate(relation.evidence_unit_ids):
            if evidence_id not in lookup:
                item_errors.append(issue(
                    relation.id, f"evidence_unit_ids.{index}", "relation_scope_violation",
                    "evidence_unit_ids must reference InfoUnits in the current Relation scope"))
        source = lookup.get(relation.source_id)
        target = lookup.get(relation.target_id)
        if relation.relation_type == RelationType.support and target is not None \
                and target.type == InfoUnitType.entity:
            item_errors.append(issue(
                relation.id, "target_id", "illegal_relation_endpoint",
                "support target must not be an entity"))
        if relation.relation_type == RelationType.contradict:
            if source is not None and source.type == InfoUnitType.entity:
                item_errors.append(issue(
                    relation.id, "source_id", "illegal_relation_endpoint",
                    "contradict source must not be an entity"))
            if target is not None and target.type == InfoUnitType.entity:
                item_errors.append(issue(
                    relation.id, "target_id", "illegal_relation_endpoint",
                    "contradict target must not be an entity"))
        key = relation_key(relation)
        if key in existing_keys or key in seen_keys:
            item_errors.append(issue(
                relation.id, "$", "duplicate_relation",
                "An equivalent Relation already exists or was already accepted in this batch"))
        if item_errors:
            errors.extend(item_errors)
            continue
        seen_keys.add(key)
        accepted.append(relation)
    return accepted, errors
