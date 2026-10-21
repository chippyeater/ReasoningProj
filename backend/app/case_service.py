"""v0.3 upload/persistence services, independent of model execution."""
from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from starlette.datastructures import UploadFile
from pydantic import TypeAdapter, ValidationError

from app import file_parsers
from app.api_models import CreateInfoUnitInput, UpdateInfoUnitInput
from app.case_store import artifact_path, atomic_write, read_parsed_document, save_case, write_json
from app.errors import AppError
from app.quote_units import QuoteUnit, quote_catalog, quote_indexes_for_case, source_ref_from_quote_unit
from app.schemas import (
    CaseData, CaseFile, ConflictView, ExtractionFile, ExtractionInput, HypothesisView, InfoUnit,
    ParseStatus, PipelineStage, RelationInput, RunStatus, SourceKind, SourceRef, StageRun, Text,
    TimelineView, Viewport, WorkspaceState,
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _source_refs_from_evidence_ids(case: CaseData, evidence_ids: list[str]) -> list[SourceRef]:
    if len(evidence_ids) != len(set(evidence_ids)):
        raise AppError("duplicate_evidence_id", "evidence_ids must be unique")
    catalog = quote_catalog(case)
    references: list[SourceRef] = []
    for evidence_id in evidence_ids:
        resolved = catalog.get(evidence_id)
        if resolved is None:
            raise AppError("invalid_evidence_id", f"Unknown current-case Quote Unit ID: {evidence_id}")
        unit, file = resolved
        references.append(source_ref_from_quote_unit(case.case_id, unit, file))
    return references


def _save_info_unit_candidate(candidate: CaseData) -> CaseData:
    candidate.updated_at = now_iso()
    try:
        candidate = CaseData.model_validate(candidate.model_dump(mode="json"))
    except ValidationError as exc:
        raise AppError(
            "invalid_info_unit_change", "InfoUnit change violates the current case contract",
            details={"errors": exc.errors(include_url=False, include_input=False, include_context=False)},
        ) from exc
    save_case(candidate)
    return candidate


def list_quote_units(case: CaseData) -> list[QuoteUnit]:
    return [unit for index in quote_indexes_for_case(case) for unit in index.units]


def evidence_ids_for_info_unit(case: CaseData, info_unit: InfoUnit) -> list[str]:
    catalog = quote_catalog(case)
    by_source = {
        (file.file_id, unit.start, unit.end, unit.text, file.source_kind): quote_id
        for quote_id, (unit, file) in catalog.items()
    }
    result: list[str] = []
    for ref in info_unit.source_refs:
        quote_id = by_source.get((ref.file_id, ref.start, ref.end, ref.quote, ref.source_kind))
        if quote_id is None:
            raise AppError(
                "info_unit_source_not_indexed",
                f"{info_unit.id}: an existing SourceRef no longer maps to a current Quote Unit",
                409,
            )
        result.append(quote_id)
    return result


def prepare_info_unit_creation(case: CaseData, payload: CreateInfoUnitInput) -> CaseData:
    references = _source_refs_from_evidence_ids(case, payload.evidence_ids)
    existing_ids = {unit.id for unit in case.info_units}
    unit_id = f"info-{uuid4().hex}"
    while unit_id in existing_ids:
        unit_id = f"info-{uuid4().hex}"
    try:
        unit = InfoUnit(
            **payload.model_dump(exclude={"evidence_ids"}), id=unit_id,
            source_refs=references, created_at=now_iso(),
        )
    except ValidationError as exc:
        raise AppError(
            "invalid_info_unit", "InfoUnit violates the current schema",
            details={"errors": exc.errors(include_url=False, include_input=False, include_context=False)},
        ) from exc
    candidate = case.model_copy(deep=True)
    candidate.info_units.append(unit)
    try:
        return CaseData.model_validate(candidate.model_dump(mode="json"))
    except ValidationError as exc:
        raise AppError(
            "invalid_info_unit_change", "InfoUnit change violates the current case contract",
            details={"errors": exc.errors(include_url=False, include_input=False, include_context=False)},
        ) from exc


def create_info_unit(case: CaseData, payload: CreateInfoUnitInput) -> CaseData:
    return _save_info_unit_candidate(prepare_info_unit_creation(case, payload))


def prepare_info_unit_update(case: CaseData, info_unit_id: str, payload: UpdateInfoUnitInput) -> CaseData:
    index = next((i for i, unit in enumerate(case.info_units) if unit.id == info_unit_id), None)
    if index is None:
        raise AppError("info_unit_not_found", f"InfoUnit not found: {info_unit_id}", 404)
    current = case.info_units[index]
    values = current.model_dump(mode="json")
    values.update(payload.model_dump(mode="json", exclude_unset=True, exclude={"evidence_ids"}))
    if "evidence_ids" in payload.model_fields_set:
        values["source_refs"] = [
            ref.model_dump(mode="json")
            for ref in _source_refs_from_evidence_ids(case, payload.evidence_ids or [])
        ]
    # Transport input cannot replace either stable field; assign them explicitly.
    values["id"] = current.id
    values["created_at"] = current.created_at
    try:
        updated = InfoUnit.model_validate(values)
    except ValidationError as exc:
        raise AppError(
            "invalid_info_unit", "InfoUnit violates the current schema",
            details={"errors": exc.errors(include_url=False, include_input=False, include_context=False)},
        ) from exc
    candidate = case.model_copy(deep=True)
    candidate.info_units[index] = updated
    try:
        return CaseData.model_validate(candidate.model_dump(mode="json"))
    except ValidationError as exc:
        raise AppError(
            "invalid_info_unit_change", "InfoUnit change violates the current case contract",
            details={"errors": exc.errors(include_url=False, include_input=False, include_context=False)},
        ) from exc


def update_info_unit(case: CaseData, info_unit_id: str, payload: UpdateInfoUnitInput) -> CaseData:
    return _save_info_unit_candidate(prepare_info_unit_update(case, info_unit_id, payload))


def _business_references_to_info_unit(case: CaseData, info_unit_id: str) -> list[dict[str, str]]:
    references: list[dict[str, str]] = []
    for unit in case.info_units:
        if unit.id == info_unit_id:
            continue
        for field in ("actor", "target"):
            if getattr(unit, field) == info_unit_id:
                references.append({"kind": "info_unit", "id": unit.id, "field": field})
    for relation in case.relations:
        for field in ("source_id", "target_id"):
            if getattr(relation, field) == info_unit_id:
                references.append({"kind": "relation", "id": relation.id, "field": field})
        if info_unit_id in relation.evidence_unit_ids:
            references.append({"kind": "relation", "id": relation.id, "field": "evidence_unit_ids"})
    for view in case.reasoning_views:
        payload = view.payload
        if isinstance(payload, ConflictView):
            for section in payload.sections:
                for item in section.items:
                    if item.info_unit_id == info_unit_id:
                        references.append({
                            "kind": "reasoning_view", "id": view.id,
                            "field": f"sections.{section.id}.items",
                        })
        elif isinstance(payload, TimelineView):
            for event in payload.events:
                if info_unit_id in event.related_info_unit_ids:
                    references.append({
                        "kind": "reasoning_view", "id": view.id,
                        "field": f"events.{event.id}.related_info_unit_ids",
                    })
        elif isinstance(payload, HypothesisView):
            for step in payload.steps:
                for field in ("supporting_info_unit_ids", "opposing_info_unit_ids"):
                    if info_unit_id in getattr(step, field):
                        references.append({
                            "kind": "reasoning_view", "id": view.id,
                            "field": f"steps.{step.id}.{field}",
                        })
    for match in case.law_matches:
        if info_unit_id in match.related_info_unit_ids:
            references.append({
                "kind": "law_match", "id": match.law_id, "field": "related_info_unit_ids",
            })
    return references


def delete_info_unit(case: CaseData, info_unit_id: str) -> CaseData:
    index = next((i for i, unit in enumerate(case.info_units) if unit.id == info_unit_id), None)
    if index is None:
        raise AppError("info_unit_not_found", f"InfoUnit not found: {info_unit_id}", 404)
    references = _business_references_to_info_unit(case, info_unit_id)
    if references:
        raise AppError(
            "info_unit_in_use", "InfoUnit has business references and cannot be deleted", 409,
            details={"references": references},
        )
    candidate = case.model_copy(deep=True)
    candidate.info_units.pop(index)
    state = candidate.workspace_state
    state.selected_node_ids = [node_id for node_id in state.selected_node_ids if node_id != info_unit_id]
    if state.focused_node_id == info_unit_id:
        state.focused_node_id = None
    state.node_positions.pop(info_unit_id, None)
    state.display_levels.pop(info_unit_id, None)
    return _save_info_unit_candidate(candidate)


def create_empty_case(case_title: str) -> CaseData:
    now = now_iso()
    return CaseData(
        case_id=f"case-{uuid4().hex}", case_title=case_title,
        files=[], core_issue=None, issue_set=[], info_units=[], relations=[],
        reasoning_views=[], law_matches=[], stage_runs=[],
        workspace_state=WorkspaceState(
            selected_node_ids=[], focused_node_id=None, current_view_id=None,
            node_positions={}, display_levels={},
            viewport=Viewport(zoom=1, offset_x=0, offset_y=0)),
        created_at=now, updated_at=now)


async def append_files_to_case(case: CaseData, uploads: Sequence[UploadFile],
                               source_kinds: dict[str, SourceKind]) -> CaseData:
    if not uploads:
        raise AppError("no_uploads", "At least one file is required")
    kinds = TypeAdapter(dict[Text, SourceKind]).validate_python(source_kinds)
    names = [u.filename for u in uploads]
    if any(not name or not name.strip() for name in names):
        raise AppError("missing_filename", "Every uploaded file requires a filename")
    if len(names) != len(set(names)):
        raise AppError("duplicate_upload_filename", "Filenames must be unique within one upload batch")
    if set(names) != kinds.keys():
        raise AppError("source_metadata_mismatch", "source_kinds keys must exactly match this batch's filenames")
    candidate = case.model_copy(deep=True)
    created: list[Path] = []
    try:
        for upload in uploads:
            filename = upload.filename
            if not filename or not filename.strip():
                raise AppError("missing_filename", "Every uploaded file requires a filename")
            # Reading errors abort the request; they are not document parse errors.
            raw = await upload.read()
            file_id = f"file-{uuid4().hex}"
            file_type = file_parsers.file_type_for(filename, upload.content_type)
            suffix = Path(filename).suffix.lower()
            # Unsupported names never control a filesystem path.
            if not suffix.isascii() or not suffix[1:].isalnum():
                suffix = ".bin"
            original = f"upload_files/{file_id}{suffix}"
            original_target = artifact_path(case.case_id, original)
            created.append(original_target)
            atomic_write(original_target, raw)
            text_path = None
            error_message = None
            try:
                text = file_parsers.parse_text(file_type, raw)
            except AppError as exc:
                status = ParseStatus.unsupported if exc.code == "unsupported_file" else ParseStatus.failed
                error_message = str(exc)
            else:
                status = ParseStatus.parsed
                text_path = f"parsed/{file_id}.txt"
                text_target = artifact_path(case.case_id, text_path)
                created.append(text_target)
                # Binary UTF-8 write fixes LF bytes and therefore character offsets.
                atomic_write(text_target, text.encode("utf-8"))
            now = now_iso()
            file = CaseFile(
                file_id=file_id, filename=filename, file_type=file_type, source_kind=kinds[filename], file_size=len(raw),
                original_path=original, parsed_text_path=text_path, parse_status=status,
                error_message=error_message, uploaded_at=now)
            run_id = f"run-{uuid4().hex}"
            input_path = f"runs/{run_id}.input.json"
            output_path = f"runs/{run_id}.output.json"
            for path, payload in (
                (input_path, {"file_id": file_id, "original_path": original}),
                (output_path, file.model_dump(mode="json")),
            ):
                target = artifact_path(case.case_id, path)
                created.append(target)
                write_json(target, payload)
            candidate.files.append(file)
            candidate.stage_runs.append(StageRun(
                id=run_id, stage=PipelineStage.parse,
                status=RunStatus.success if status == ParseStatus.parsed else RunStatus.failed,
                model=None, prompt_version=None, input_snapshot_path=input_path,
                output_snapshot_path=output_path, error_message=error_message, created_at=now))
        candidate.updated_at = now_iso()
        save_case(candidate)
    except Exception:
        # Failed append never mutates the caller's case or leaves partial artifacts.
        for target in reversed(created):
            target.unlink(missing_ok=True)
        for directory in dict.fromkeys(target.parent for target in reversed(created)):
            if directory.exists() and not any(directory.iterdir()):
                directory.rmdir()
        if created:
            root = created[0].parent.parent
            if root.exists() and not any(root.iterdir()):
                root.rmdir()
        raise
    return candidate


async def create_case_from_uploads(case_title: str, files: Sequence[UploadFile],
                                  source_kinds: dict[str, SourceKind]) -> CaseData:
    return await append_files_to_case(create_empty_case(case_title), files, source_kinds)


def build_extraction_input(case: CaseData, file_ids: list[str] | None = None) -> ExtractionInput:
    files = {file.file_id: file for file in case.files}
    selected = list(files) if file_ids is None else file_ids
    if not selected or len(selected) != len(set(selected)):
        raise AppError("invalid_file_selection", "Select at least one file without duplicate IDs")
    unknown = set(selected) - files.keys()
    if unknown:
        raise AppError("unknown_file", f"Unknown case file IDs: {sorted(unknown)}")
    return ExtractionInput(case_title=case.case_title, files=[
        ExtractionFile(file_id=fid, filename=files[fid].filename,
                       text=read_parsed_document(case.case_id, files[fid]).text)
        for fid in selected])


def build_relation_input(case: CaseData) -> RelationInput:
    return RelationInput(core_issue=case.core_issue, issue_set=case.issue_set, info_units=case.info_units)
