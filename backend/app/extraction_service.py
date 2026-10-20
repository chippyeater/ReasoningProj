"""TXT -> LLM -> validated v0.2 extraction, with an auditable atomic commit."""
from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from app import llm_client
from app.case_service import build_extraction_input, now_iso
from app.case_store import artifact_path, case_dir, load_case_snapshot, save_case, write_json
from app.errors import AppError
from app.extraction import (
    ModelConsolidation, ModelExtraction, materialize_extraction,
    parse_model_consolidation, parse_model_extraction,
)
from app.schemas import CaseData, ExtractionOutput, ParseStatus, PipelineStage, RunStatus, StageRun

PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts/extraction_system_prompt.md"
CONSOLIDATION_PROMPT_PATH = PROMPT_PATH.with_name("extraction_consolidation_prompt.md")


def prompt_with_version() -> tuple[str, str, str]:
    try:
        prompt = PROMPT_PATH.read_text(encoding="utf-8")
        consolidation = CONSOLIDATION_PROMPT_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise AppError("extraction_prompt_unavailable", "Extraction prompt could not be read", 500) from exc
    if not prompt.strip() or not consolidation.strip():
        raise AppError("extraction_prompt_unavailable", "Extraction prompt is empty", 500)
    version = "v0.2-extraction-6:" + hashlib.sha256((prompt + "\n" + consolidation).encode("utf-8")).hexdigest()[:16]
    return prompt, consolidation, version


def _snapshot_bytes(case_id: str) -> bytes:
    return (case_dir(case_id) / "case.json").read_bytes()


def _write_audit(case_id: str, run: StageRun, audit: dict) -> None:
    write_json(artifact_path(case_id, run.output_snapshot_path), llm_client.redact(audit))
    # Standalone StageRun remains discoverable even if case artifacts are broken.
    write_json(artifact_path(case_id, f"runs/{run.id}.stage.json"), run.model_dump(mode="json"))


async def run_extraction(case: CaseData) -> ExtractionOutput:
    run_id = f"run-{uuid4().hex}"
    run = StageRun(id=run_id, stage=PipelineStage.extraction, status=RunStatus.failed,
                   model=None, prompt_version=None, input_snapshot_path=f"runs/{run_id}.input.json",
                   output_snapshot_path=f"runs/{run_id}.output.json", error_message=None, created_at=now_iso())
    audit: dict = {"run_id": run_id, "status": "started", "raw_response": None,
                   "model_content": None, "parsed_result": None, "result": None, "errors": [],
                   "active_step": "preparation", "model_request_attempts": 0}
    initial_snapshot = _snapshot_bytes(case.case_id)
    try:
        # This stage always sees every parsed file in the case; selection is not
        # used to silently bypass an otherwise valid source.
        parsed_ids = [f.file_id for f in case.files if f.parse_status == ParseStatus.parsed]
        audit["excluded_unparsed_files"] = [
            {"file_id": f.file_id, "parse_status": f.parse_status.value, "error_message": f.error_message}
            for f in case.files if f.parse_status != ParseStatus.parsed]
        if not parsed_ids:
            raise AppError("no_parsed_files", "The case has no successfully parsed files")
        input_data = build_extraction_input(case, parsed_ids)
        prompt, consolidation_prompt, version = prompt_with_version()
        run.prompt_version = version
        config = llm_client.load_config()
        run.model = config.model
        model_input = {**input_data.model_dump(mode="json"),
                       "confirmed_source_kinds": {f.file_id: f.source_kind.value for f in case.files if f.file_id in parsed_ids}}
        write_json(artifact_path(case.case_id, run.input_snapshot_path), llm_client.redact({
            "run_id": run_id, "model_input": model_input, "system_prompt": prompt,
            "consolidation_prompt": consolidation_prompt,
            "prompt_version": version, "configuration": config.public_metadata(),
            "case_snapshot_sha256": hashlib.sha256(initial_snapshot).hexdigest()}))
        audit["active_step"] = "extraction"
        audit["model_request_attempts"] = 1
        # Write the attempted step before the call, including on interruption.
        write_json(artifact_path(case.case_id, run.output_snapshot_path), audit)
        schema = ModelExtraction.model_json_schema()
        reply = await llm_client.call_structured(config, prompt, model_input, schema)
        audit["raw_response"] = reply.response
        audit["model_content"] = reply.content
        draft = parse_model_extraction(reply.content)
        audit["parsed_result"] = draft.model_dump(mode="json")
        validated_draft, draft_merges = materialize_extraction(draft, case)
        audit["validated_draft"] = validated_draft.model_dump(mode="json")
        audit["draft_merges"] = draft_merges
        if _snapshot_bytes(case.case_id) != initial_snapshot:
            raise AppError("case_changed_during_extraction", "Case changed while the model was running; result was not committed", 409)
        # An explicit always-run semantic review, not an invalid-output repair.
        # The draft must already pass both schema and source checks to reach it.
        audit["active_step"] = "consolidation"
        consolidation_input = {**model_input, "draft_extraction": draft.model_dump(mode="json")}
        consolidation_path = f"runs/{run_id}.consolidation.input.json"
        audit["consolidation_input_path"] = consolidation_path
        write_json(artifact_path(case.case_id, consolidation_path), llm_client.redact(consolidation_input))
        audit["model_request_attempts"] = 2
        write_json(artifact_path(case.case_id, run.output_snapshot_path), llm_client.redact(audit))
        consolidated = await llm_client.call_structured(
            config, consolidation_prompt, consolidation_input, ModelConsolidation.model_json_schema())
        audit["consolidation_raw_response"] = consolidated.response
        audit["consolidation_content"] = consolidated.content
        review = parse_model_consolidation(consolidated.content, len(draft.info_units))
        audit["consolidation_review"] = review.model_dump(mode="json", exclude={"extraction"})
        consolidated_draft = review.extraction
        audit["consolidated_result"] = consolidated_draft.model_dump(mode="json")
        result, merges = materialize_extraction(consolidated_draft, case)
        audit["merges"] = merges
        audit["result"] = result.model_dump(mode="json")
        if _snapshot_bytes(case.case_id) != initial_snapshot:
            raise AppError("case_changed_during_extraction", "Case changed while the model was running; result was not committed", 409)
        candidate = case.model_copy(deep=True)
        candidate.core_issue = result.core_issue
        candidate.issue_set = result.issue_set
        candidate.info_units = result.info_units
        try:
            candidate = CaseData.model_validate(candidate.model_dump(mode="json"))
        except ValidationError as exc:
            raise AppError("extraction_state_conflict", "Replacing InfoUnits would invalidate existing case references", 409) from exc
        run.status = RunStatus.success
        audit["active_step"] = "commit"
        audit["status"] = "success"
        _write_audit(case.case_id, run, audit)
        candidate.stage_runs.append(run)
        candidate.updated_at = now_iso()
        save_case(candidate, set_current=False)
        return result
    except Exception as exc:
        if isinstance(exc, AppError):
            failure = exc
        elif isinstance(exc, OSError):
            failure = AppError("extraction_storage_failed", "Extraction artifacts could not be persisted", 500)
        else:
            failure = AppError("extraction_internal_error", "Unexpected extraction failure; see run audit", 500)
        run.status = RunStatus.failed
        run.error_message = f"{failure.code}: {failure}"
        audit["status"] = "failed"
        audit["exception_type"] = type(exc).__name__
        audit["error_code"] = failure.code
        audit["errors"] = getattr(exc, "errors", []) or [{"code": failure.code, "message": str(failure)}]
        if isinstance(exc, llm_client.LLMFailure):
            key = "consolidation_raw_response" if audit["active_step"] == "consolidation" else "raw_response"
            audit[key] = exc.response
        try:
            if not artifact_path(case.case_id, run.input_snapshot_path).exists():
                write_json(artifact_path(case.case_id, run.input_snapshot_path), {
                    "run_id": run_id, "case_id": case.case_id, "input_preparation_failed": True})
            _write_audit(case.case_id, run, audit)
            latest = load_case_snapshot(case.case_id)
            if latest is None:
                raise OSError("Case was removed during extraction")
            latest.stage_runs.append(run)
            latest.updated_at = now_iso()
            save_case(latest, set_current=False)
        except (OSError, AppError, ValidationError) as audit_exc:
            # Do not hide an audit/commit failure behind the earlier model error.
            raise AppError("extraction_audit_persistence_failed", "Extraction failed and its StageRun index could not be saved", 500,
                           details={"run_id": run_id, "cause_code": failure.code,
                                    "audit_error_type": type(audit_exc).__name__}) from audit_exc
        details = {**failure.details, "run_id": run_id, "step": audit["active_step"], "errors": audit["errors"]}
        raise AppError(failure.code, str(failure), failure.status_code, details=llm_client.redact(details)) from exc
