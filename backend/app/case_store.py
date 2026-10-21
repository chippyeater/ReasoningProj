"""Strict v0.3 snapshots plus explicit, non-persisting v0.2 migration."""
from __future__ import annotations

import copy
import json
import re
import shutil
from pathlib import Path, PurePosixPath
from uuid import uuid4

from pydantic import ValidationError

from app.errors import AppError
from app.schemas import CaseData, CaseFile, ParsedDocument, ParseStatus

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def migrate_v02_snapshot(payload: dict) -> dict:
    """Return an in-memory v0.3 projection or report every unsafe legacy choice.

    This function never writes the migrated value. Ambiguous statement semantics,
    entity-name references and temporal relations are surfaced instead of guessed.
    """
    value = copy.deepcopy(payload)
    units = value.get("info_units")
    if not isinstance(units, list):
        raise AppError("legacy_migration_invalid", "Legacy snapshot has no InfoUnit list", 409)
    files = {item.get("file_id"): item for item in value.get("files", []) if isinstance(item, dict)}
    errors: list[dict] = []
    entities: list[dict] = []
    for unit in units:
        if not isinstance(unit, dict):
            errors.append({"item_id": None, "field": "info_units", "message": "Legacy InfoUnit is not an object"})
            continue
        if unit.get("type") == "entity":
            entities.append(unit)

    def entity_id(raw: object, unit_id: object, field: str) -> object:
        if raw is None:
            return None
        if any(entity.get("id") == raw for entity in entities):
            return raw
        matches = [entity.get("id") for entity in entities
                   if isinstance(raw, str) and isinstance(entity.get("title"), str)
                   and (entity["title"] == raw or raw in entity["title"])]
        if len(matches) == 1:
            return matches[0]
        errors.append({"item_id": unit_id, "field": field,
                       "message": "Legacy name reference does not uniquely identify an entity"})
        return None

    # Old derived relations can provide a lossless ID reference before removal.
    derived: dict[str, dict[str, str]] = {}
    semantic_relations = []
    for relation in value.get("relations", []):
        kind = relation.get("relation_type") if isinstance(relation, dict) else None
        if kind in {"actor_of", "target_of"}:
            derived.setdefault(relation.get("source_id"), {})[
                "actor" if kind == "actor_of" else "target"] = relation.get("target_id")
        elif kind == "occur_at":
            errors.append({"item_id": relation.get("id"), "field": "relation_type",
                           "message": "Legacy occur_at requires semantic review and cannot be migrated automatically"})
        else:
            semantic_relations.append(relation)
    value["relations"] = semantic_relations

    for unit in units:
        if not isinstance(unit, dict):
            continue
        unit_id = unit.get("id")
        old_type, old_subtype = unit.get("type"), unit.get("subtype")
        unit.pop("summary", None)
        unit["related_issue"] = unit.pop("stance_target", unit.get("related_issue"))
        if old_type == "state":
            unit["subtype"] = None
            if not isinstance(unit.get("speaker"), str) or not unit["speaker"].strip():
                names = {files.get(ref.get("file_id"), {}).get("filename")
                         for ref in unit.get("source_refs", []) if isinstance(ref, dict)}
                names.discard(None)
                if len(names) == 1:
                    unit["speaker"] = next(iter(names))
                else:
                    errors.append({"item_id": unit_id, "field": "speaker",
                                   "message": "Legacy state has no unambiguous real source owner"})
        elif old_type == "event":
            source_kinds = {ref.get("source_kind") for ref in unit.get("source_refs", []) if isinstance(ref, dict)}
            # A generic document may itself be a party narrative. Only the
            # dedicated record provenance is strong enough for a lossless
            # automatic evidence-stage upgrade; every other legacy event is
            # conservatively retained as reported.
            unit["subtype"] = "documented" if source_kinds == {"record"} else "reported"
        elif old_type == "statement" and old_subtype == "provision":
            unit["type"], unit["subtype"] = "rule", None
        elif old_type == "statement":
            errors.append({"item_id": unit_id, "field": "type",
                           "message": f"Legacy statement/{old_subtype} needs semantic review between event, state and position"})
        elif old_type != "entity":
            errors.append({"item_id": unit_id, "field": "type", "message": "Unknown legacy InfoUnit type"})

        for field in ("actor", "target"):
            raw = derived.get(unit_id, {}).get(field, unit.get(field))
            unit[field] = entity_id(raw, unit_id, field)

    if errors:
        raise AppError("legacy_migration_ambiguous",
                       "Stored v0.2 case requires explicit semantic migration before v0.3 use", 409,
                       details={"migration_errors": errors})
    return value


def case_dir(case_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", case_id):
        raise AppError("invalid_case_id", "case_id must contain only letters, digits, _ or -")
    root = DATA_DIR.resolve()
    target = (root / case_id).resolve()
    if target.parent != root or target.name != case_id:
        raise AppError("invalid_case_path", "Case path escapes the data directory")
    return target


def artifact_path(case_id: str, relative: str) -> Path:
    path = PurePosixPath(relative)
    if (path.is_absolute() or ".." in path.parts or "\\" in relative
            or ":" in relative or not relative or str(path) != relative):
        raise AppError("invalid_artifact_path", "Expected a normalized relative artifact path")
    root = case_dir(case_id)
    target = (root / relative).resolve()
    if not target.is_relative_to(root) or target == root:
        raise AppError("invalid_artifact_path", "Artifact path escapes the case directory")
    return target


def atomic_write(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_bytes(raw)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def write_json(path: Path, payload: object) -> None:
    atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))


def read_parsed_document(case_id: str, file: CaseFile) -> ParsedDocument:
    if file.parse_status != ParseStatus.parsed or file.parsed_text_path is None:
        raise AppError("file_not_parsed", f"{file.file_id}: file is {file.parse_status.value}")
    try:
        text = artifact_path(case_id, file.parsed_text_path).read_bytes().decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise AppError("parsed_cache_unavailable", f"{file.file_id}: cannot read cached UTF-8 TXT", 409) from exc
    if not text.strip():
        raise AppError("parsed_cache_empty", f"{file.file_id}: cached TXT is empty", 409)
    return ParsedDocument(file_id=file.file_id, text=text)


def _validate_artifacts(case: CaseData) -> None:
    for file in case.files:
        if not artifact_path(case.case_id, file.original_path).is_file():
            raise AppError("original_file_missing", f"{file.file_id}: original file is missing", 409)
    # Cache integrity is mandatory even for a case without extracted units.
    documents = {
        f.file_id: read_parsed_document(case.case_id, f).text
        for f in case.files if f.parse_status == ParseStatus.parsed
    }
    for unit in case.info_units:
        for ref in unit.source_refs:
            text = documents[ref.file_id]
            if ref.end > len(text) or text[ref.start:ref.end] != ref.quote:
                raise AppError("source_quote_mismatch", f"{unit.id}: quote does not match cached TXT", 409)


def save_case(case: CaseData, set_current: bool = True) -> Path:
    # Revalidate because service code may have modified nested lists in place.
    case = CaseData.model_validate(case.model_dump(mode="json"))
    _validate_artifacts(case)
    target = case_dir(case.case_id) / "case.json"
    old_snapshot = target.read_bytes() if target.exists() else None
    write_json(target, case.model_dump(mode="json"))
    if set_current:
        try:
            atomic_write(DATA_DIR / "current_case_id.txt", case.case_id.encode("utf-8"))
        except OSError:
            if old_snapshot is None:
                target.unlink(missing_ok=True)
            else:
                atomic_write(target, old_snapshot)
            raise
    return target


def load_case_snapshot(case_id: str) -> CaseData | None:
    """Read v0.3, or safely project an unambiguous v0.2 snapshot in memory."""
    target = case_dir(case_id) / "case.json"
    if not target.exists():
        return None
    try:
        raw = target.read_bytes()
        try:
            case = CaseData.model_validate_json(raw)
        except ValidationError:
            legacy = json.loads(raw)
            case = CaseData.model_validate(migrate_v02_snapshot(legacy))
    except AppError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, ValidationError) as exc:
        raise AppError("invalid_case_data", f"{case_id}: stored snapshot is not valid v0.3 data", 409) from exc
    if case.case_id != case_id:
        raise AppError("case_id_mismatch", "Stored case_id differs from the requested case", 409)
    return case


def load_case(case_id: str) -> CaseData | None:
    case = load_case_snapshot(case_id)
    if case is not None:
        _validate_artifacts(case)
    return case


def list_cases() -> list[CaseData]:
    if not DATA_DIR.exists():
        return []
    cases = []
    for directory in sorted(DATA_DIR.iterdir()):
        if directory.is_dir() and (directory / "case.json").exists():
            case = load_case(directory.name)
            if case is not None:
                cases.append(case)
    return sorted(cases, key=lambda c: c.updated_at, reverse=True)


def load_current_case() -> CaseData | None:
    pointer = DATA_DIR / "current_case_id.txt"
    if not pointer.exists():
        return None
    current_id = pointer.read_text(encoding="utf-8").strip()
    if not current_id:
        raise AppError("invalid_current_case", "Current case pointer is empty", 409)
    case = load_case(current_id)
    if case is None:
        raise AppError("current_case_missing", "Current case pointer refers to a missing case", 409)
    return case


def set_current_case(case_id: str) -> bool:
    if load_case(case_id) is None:
        return False
    atomic_write(DATA_DIR / "current_case_id.txt", case_id.encode("utf-8"))
    return True


def clear_case(case_id: str) -> None:
    target = case_dir(case_id)
    # case_dir resolves and verifies the absolute deletion target first.
    if target.exists():
        shutil.rmtree(target)
    pointer = DATA_DIR / "current_case_id.txt"
    if pointer.exists() and pointer.read_text(encoding="utf-8").strip() == case_id:
        pointer.unlink()


def clear_current_case() -> None:
    case = load_current_case()
    if case is not None:
        clear_case(case.case_id)
