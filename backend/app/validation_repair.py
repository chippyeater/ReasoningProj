"""Small shared primitives for item-level validation and one-shot repair loops."""
from __future__ import annotations

import json
from typing import Any, Iterable

from pydantic import ValidationError

from app.errors import AppError


BATCH_ITEM_ID = "__batch__"


def issue(item_id: str, field_path: str, error_code: str, message: str) -> dict[str, str]:
    return {
        "item_id": item_id,
        "field_path": field_path,
        "error_code": error_code,
        "message": message,
    }


def _unique_json_keys(pairs: list[tuple[str, object]]) -> dict:
    value: dict = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"Duplicate JSON key: {key}")
        value[key] = item
    return value


def _invalid_json_constant(value: str) -> None:
    raise ValueError(f"Non-JSON constant: {value}")


def strict_json_object(raw: str, *, code: str, label: str) -> dict[str, Any]:
    """Parse strict JSON while rejecting duplicate keys and non-object envelopes."""
    try:
        value = json.loads(
            raw, object_pairs_hook=_unique_json_keys, parse_constant=_invalid_json_constant)
    except (TypeError, ValueError) as exc:
        raise AppError(code, f"{label} is not strict JSON", 422, details={"errors": [
            issue(BATCH_ITEM_ID, "$", "invalid_json", str(exc)),
        ]}) from exc
    if not isinstance(value, dict):
        raise AppError(code, f"{label} must be a JSON object", 422, details={"errors": [
            issue(BATCH_ITEM_ID, "$", "object_required", "Top-level JSON value must be an object"),
        ]})
    return value


def require_exact_fields(value: dict[str, Any], expected: set[str], *, code: str, label: str) -> None:
    missing = sorted(expected - value.keys())
    extra = sorted(value.keys() - expected)
    if not missing and not extra:
        return
    errors = [issue(BATCH_ITEM_ID, field, "missing", "Required field is missing") for field in missing]
    errors.extend(issue(BATCH_ITEM_ID, field, "extra_forbidden", "Unexpected field") for field in extra)
    raise AppError(code, f"{label} has an invalid batch envelope", 422, details={"errors": errors})


def locatable_items(value: object, *, list_field: str, id_field: str,
                    code: str, label: str) -> list[tuple[str, dict[str, Any]]]:
    """Return stable item IDs or reject the batch when safe item attribution is impossible."""
    if not isinstance(value, list):
        raise AppError(code, f"{label}.{list_field} must be an array", 422, details={"errors": [
            issue(BATCH_ITEM_ID, list_field, "list_required", "Expected an array"),
        ]})
    located: list[tuple[str, dict[str, Any]]] = []
    errors: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, raw_item in enumerate(value):
        path = f"{list_field}.{index}"
        if not isinstance(raw_item, dict):
            errors.append(issue(BATCH_ITEM_ID, path, "object_required", "Candidate must be an object"))
            continue
        item_id = raw_item.get(id_field)
        if not isinstance(item_id, str) or not item_id.strip():
            errors.append(issue(BATCH_ITEM_ID, f"{path}.{id_field}", "unlocatable_item",
                                f"Candidate requires a nonempty string {id_field}"))
            continue
        if item_id in seen:
            errors.append(issue(BATCH_ITEM_ID, f"{path}.{id_field}", "duplicate_item_id",
                                f"Duplicate candidate ID: {item_id}"))
            continue
        seen.add(item_id)
        located.append((item_id, raw_item))
    if errors:
        raise AppError(code, f"{label} contains candidates that cannot be safely located", 422,
                       details={"errors": errors})
    return located


def pydantic_issues(item_id: str, exc: ValidationError, *, prefix: str = "") -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    for error in exc.errors(include_url=False, include_input=False, include_context=False):
        parts = [str(part) for part in error.get("loc", ()) if str(part) not in {
            "ModelEntity", "ModelEvent", "ModelState", "ModelRule", "ModelPosition",
        }]
        path = ".".join(parts)
        field_path = ".".join(part for part in (prefix, path) if part) or "$"
        errors.append(issue(item_id, field_path, str(error.get("type") or "schema_invalid"),
                            str(error.get("msg") or "Schema validation failed")))
    return errors


def item_errors(errors: Iterable[dict[str, str]]) -> dict[str, list[dict[str, str]]]:
    grouped: dict[str, list[dict[str, str]]] = {}
    for error in errors:
        grouped.setdefault(error["item_id"], []).append(error)
    return grouped


def repair_items(ordered_ids: Iterable[str], raw_by_id: dict[str, dict[str, Any]],
                 errors: Iterable[dict[str, str]]) -> list[dict[str, Any]]:
    """Build the common minimal failed-item envelope in deterministic order."""
    grouped = item_errors(errors)
    return [
        {"original": raw_by_id[item_id], "errors": grouped[item_id]}
        for item_id in ordered_ids if item_id in grouped
    ]


def merge_repair_results(
        ordered_ids: Iterable[str], raw_by_id: dict[str, dict[str, Any]],
        failed_ids: set[str], repaired_by_id: dict[str, dict[str, Any]],
        discarded_ids: set[str],
        ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Replace only failed items and return the full revalidation batch plus its diff."""
    combined: list[dict[str, Any]] = []
    changes: list[dict[str, Any]] = []
    for item_id in ordered_ids:
        if item_id in discarded_ids:
            continue
        item = repaired_by_id[item_id] if item_id in failed_ids else raw_by_id[item_id]
        combined.append(item)
        if item_id in repaired_by_id:
            changes.extend(field_changes(raw_by_id[item_id], item, item_id=item_id))
    return combined, changes


def validate_resolution(expected_ids: set[str], repaired_ids: set[str], rejected_ids: set[str],
                        *, code: str, repaired_field: str, rejected_field: str) -> None:
    overlap = repaired_ids & rejected_ids
    resolved = repaired_ids | rejected_ids
    if not overlap and resolved == expected_ids:
        return
    raise AppError(code, "Repair output must resolve every failed candidate exactly once", 422,
                   details={"errors": [issue(
                       BATCH_ITEM_ID, "$", "repair_resolution_mismatch",
                       f"expected={sorted(expected_ids)}, {repaired_field}={sorted(repaired_ids)}, "
                       f"{rejected_field}={sorted(rejected_ids)}, overlap={sorted(overlap)}",
                   )]})


def field_changes(before: dict[str, Any], after: dict[str, Any], *, item_id: str) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for field in sorted(before.keys() | after.keys()):
        if before.get(field) != after.get(field):
            changes.append({
                "item_id": item_id,
                "field_path": field,
                "before": before.get(field),
                "after": after.get(field),
            })
    return changes
