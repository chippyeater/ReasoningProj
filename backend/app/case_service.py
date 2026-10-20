"""v0.2 upload/persistence services, independent of model execution."""
from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from starlette.datastructures import UploadFile
from pydantic import TypeAdapter

from app import file_parsers
from app.case_store import artifact_path, atomic_write, read_parsed_document, save_case, write_json
from app.errors import AppError
from app.schemas import (
    CaseData, CaseFile, ExtractionFile, ExtractionInput, ParseStatus,
    PipelineStage, RelationInput, RunStatus, SourceKind, StageRun, Text, Viewport, WorkspaceState,
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


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

