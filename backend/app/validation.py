"""Validate v0.3 results against fixed cached text and confirmed provenance."""
from __future__ import annotations

from app.case_store import read_parsed_document
from app.errors import AppError
from app.schemas import (
    CaseFile, ExtractionOutput, RelationInput, RelationOutput,
    SourceRef, ValidationErrorItem, ValidationInput, ValidationResult, check_relations,
)


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
    try:
        check_relations(context.info_units, value.relations)
    except ValueError as exc:
        return ValidationResult[RelationOutput](valid=False, data=None, errors=[
            ValidationErrorItem(item_id=None, field="relations", message=str(exc))])
    return ValidationResult[RelationOutput](valid=True, data=value, errors=[])
