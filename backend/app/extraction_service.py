"""TXT -> LLM -> validated v0.3 extraction, with an auditable atomic commit."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from app import llm_client
from app.observability import RunTrace
from app.case_service import now_iso
from app.case_store import artifact_path, case_dir, load_case_snapshot, save_case, write_json
from app.errors import AppError
from app.extraction import (
    ExtractionFailure, ExtractionValidationRepair, ModelExtraction, extraction_repair_evidence,
    materialize_extraction, parse_extraction_payload, validate_extraction_candidates,
)
from app.quote_units import QuoteIndexEvent, build_quote_model_input
from app.prompt_loader import LoadedPrompt, load_prompt
from app.schemas import CaseData, ExtractionOutput, ParseStatus, PipelineStage, RunStatus, StageRun
from app.validation_repair import (
    BATCH_ITEM_ID, locatable_items, merge_repair_results, repair_items,
    require_exact_fields, strict_json_object, validate_resolution,
)

PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts/extraction_system_prompt.md"
VALIDATION_REPAIR_PROMPT_PATH = PROMPT_PATH.with_name("extraction_validation_repair_prompt.md")


def load_prompts() -> tuple[LoadedPrompt, LoadedPrompt]:
    try:
        prompt = load_prompt(PROMPT_PATH, "extraction")
        repair = load_prompt(VALIDATION_REPAIR_PROMPT_PATH, "extraction_validation_repair")
    except (OSError, UnicodeError, ValueError) as exc:
        raise AppError(
            "extraction_prompt_unavailable", f"Extraction prompt could not be loaded: {exc}", 500) from exc
    return prompt, repair


def _model_call_record(step: str, prompt: LoadedPrompt,
                       config: llm_client.LLMConfig) -> dict[str, str | None]:
    return {"step": step, **prompt.metadata(config.model)}


def _snapshot_bytes(case_id: str) -> bytes:
    return (case_dir(case_id) / "case.json").read_bytes()


def _write_audit(case_id: str, run: StageRun, audit: dict) -> None:
    write_json(artifact_path(case_id, run.output_snapshot_path), llm_client.redact(audit))
    # Standalone StageRun remains discoverable even if case artifacts are broken.
    write_json(artifact_path(case_id, f"runs/{run.id}.stage.json"), run.model_dump(mode="json"))


async def _validate_repair_and_materialize(
        payload: dict[str, Any], case: CaseData, *, config: llm_client.LLMConfig,
        repair_prompt: LoadedPrompt, run: StageRun, audit: dict,
        trace: RunTrace) -> tuple[ExtractionOutput, list[dict]]:
    with trace.observation(
            "Extraction · validate candidates", as_type="guardrail", input=payload) as validation_span:
        initial_report = validate_extraction_candidates(payload, case)
        initial_validation = initial_report.audit_payload()
        validation_span.update(
            output=initial_validation,
            level="ERROR" if initial_report.errors else None,
            status_message="extraction_candidates_invalid" if initial_report.errors else None,
        )
    audit["initial_validation"] = initial_validation
    final_report = initial_report
    unresolved_ids: set[str] = set()

    if initial_report.errors:
        failed_ids = initial_report.failed_ids
        failed_items = [
            initial_report.raw_by_id[item_id]
            for item_id in initial_report.ordered_ids if item_id in failed_ids
        ]
        repair_input = {
            "failed_items": repair_items(
                initial_report.ordered_ids, initial_report.raw_by_id, initial_report.errors),
            "evidence_candidates": extraction_repair_evidence(case, failed_items),
            "entity_dependencies": [
                {
                    "local_id": item.local_id, "type": item.type,
                    "subtype": item.subtype,
                    "title": item.title, "content": item.content,
                }
                for item in initial_report.models_by_id.values() if item.type == "entity"
            ],
            "batch_constraints": {
                "core_issue": initial_report.core_issue,
                "issue_set": initial_report.issue_set,
                "allowed_types": ["entity", "event", "state", "rule", "position"],
                "actor_target_must_reference_entity_local_id": True,
                "state_position_require_speaker": True,
            },
        }
        repair_path = f"runs/{run.id}.validation-repair.input.json"
        write_json(artifact_path(case.case_id, repair_path), llm_client.redact(repair_input))
        audit["active_step"] = "extraction_validation_repair"
        audit["model_request_attempts"] = 2
        audit["repair"] = {
            "input_path": repair_path,
            "failed_ids": [item_id for item_id in initial_report.ordered_ids if item_id in failed_ids],
            "raw_response": None, "content": None, "changes": [],
        }
        audit["model_calls"].append(_model_call_record(
            "extraction_validation_repair", repair_prompt, config))
        _write_audit(case.case_id, run, audit)
        repair_schema = ExtractionValidationRepair.model_json_schema()
        with trace.generation(
                "Extraction Validation Repair · model call", config=config,
                prompt=repair_prompt, model_input=repair_input, schema=repair_schema) as generation:
            repair_reply = await llm_client.call_structured(
                config, repair_prompt.content, repair_input, repair_schema)
            generation.record_reply(repair_reply)
        audit["repair"]["raw_response"] = repair_reply.response
        audit["repair"]["content"] = repair_reply.content
        with trace.observation(
                "Extraction Validation Repair · parse", as_type="evaluator",
                input=repair_reply.content) as parse_span:
            repair_payload = strict_json_object(
                repair_reply.content, code="extraction_repair_json_invalid",
                label="Extraction repair output")
            require_exact_fields(
                repair_payload, {"repairs", "unresolved_ids"},
                code="extraction_repair_schema_invalid", label="Extraction repair output")
            repaired_located = locatable_items(
                repair_payload["repairs"], list_field="repairs", id_field="local_id",
                code="extraction_repair_schema_invalid", label="Extraction repair output")
            unresolved_value = repair_payload["unresolved_ids"]
            if (not isinstance(unresolved_value, list)
                    or any(not isinstance(item, str) or not item.strip() for item in unresolved_value)
                    or len(unresolved_value) != len(set(unresolved_value))):
                raise AppError(
                    "extraction_repair_schema_invalid",
                    "unresolved_ids must be a unique list of nonempty strings", 422)
            repaired_raw = {item_id: raw_item for item_id, raw_item in repaired_located}
            unresolved_ids = set(unresolved_value)
            validate_resolution(
                failed_ids, set(repaired_raw), unresolved_ids,
                code="extraction_repair_schema_invalid", repaired_field="repairs",
                rejected_field="unresolved_ids")
            parse_span.update(output=repair_payload)
        combined_items, changes = merge_repair_results(
            initial_report.ordered_ids, initial_report.raw_by_id, failed_ids,
            repaired_raw, unresolved_ids)
        audit["repair"]["changes"] = changes
        combined_payload = {
            "core_issue": initial_report.core_issue,
            "issue_set": initial_report.issue_set,
            "info_units": combined_items,
        }
        with trace.observation(
                "Extraction Validation Repair · resolution", as_type="evaluator",
                input=repair_payload) as resolution_span:
            resolution_span.update(output={
                "changes": changes, "unresolved_ids": sorted(unresolved_ids),
            })
        with trace.observation(
                "Extraction Validation Repair · revalidate", as_type="guardrail",
                input=combined_payload) as revalidation_span:
            final_report = validate_extraction_candidates(combined_payload, case)
            revalidation = final_report.audit_payload()
            revalidation_span.update(
                output=revalidation,
                level="ERROR" if final_report.errors else None,
                status_message="extraction_revalidation_has_rejections" if final_report.errors else None,
            )
        audit["revalidation"] = revalidation

    rejected_ids = unresolved_ids | final_report.failed_ids
    audit["discarded_candidates"] = [
        {
            "local_id": item_id,
            "original": initial_report.raw_by_id.get(item_id),
            "errors": [error for error in [*initial_report.errors, *final_report.errors]
                       if error["item_id"] == item_id],
            "unresolved": item_id in unresolved_ids,
        }
        for item_id in initial_report.ordered_ids if item_id in rejected_ids
    ]
    audit["accepted_count"] = len(final_report.accepted_ids)
    audit["discarded_count"] = len(rejected_ids)
    if not final_report.accepted_ids:
        raise AppError(
            "extraction_no_acceptable_candidates",
            "Extraction produced no safely acceptable InfoUnits", 422,
            details={"errors": [*initial_report.errors, *final_report.errors]})
    draft = final_report.accepted_draft()
    diagnostics: dict = {}
    with trace.observation(
            "Extraction · materialize accepted candidates", as_type="guardrail",
            input=draft.model_dump(mode="json")) as materialize_span:
        try:
            result, merges = materialize_extraction(draft, case, diagnostics)
        except ExtractionFailure as exc:
            materialize_span.update(
                output={"valid": False, "diagnostics": exc.diagnostics or diagnostics,
                        "errors": exc.errors},
                level="ERROR", status_message=exc.code)
            raise
        materialize_span.update(output={
            "valid": True, "diagnostics": diagnostics, "merges": merges})
    audit["materialization"] = diagnostics
    return result, merges


async def run_extraction(case: CaseData, *, workflow_id: str | None = None) -> ExtractionOutput:
    run_id = f"run-{uuid4().hex}"
    workflow_id = workflow_id or f"workflow-{uuid4().hex}"
    run = StageRun(id=run_id, stage=PipelineStage.extraction, status=RunStatus.failed,
                   model=None, prompt_version=None, input_snapshot_path=f"runs/{run_id}.input.json",
                   output_snapshot_path=f"runs/{run_id}.output.json", error_message=None, created_at=now_iso())
    trace = RunTrace(
        case_id=case.case_id, case_title=case.case_title, stage="Extraction",
        run_id=run_id, workflow_id=workflow_id, created_at=run.created_at,
        initial_input={"case_id": case.case_id, "stage": "extraction"},
    )
    audit: dict = {"run_id": run_id, "run_name": trace.run_name, "workflow_id": workflow_id,
                   "status": "started", "result_status": None, "raw_response": None,
                   "model_content": None, "parsed_result": None, "result": None, "errors": [],
                   "active_step": "preparation", "model_request_attempts": 0,
                   "initial_validation": None, "repair": None, "revalidation": None,
                   "accepted_count": 0, "discarded_count": 0, "model_calls": []}
    initial_snapshot = _snapshot_bytes(case.case_id)
    try:
        # This stage always sees every parsed file in the case; selection is not
        # used to silently bypass an otherwise valid source.
        if case.info_units:
            raise AppError(
                "extraction_existing_results_conflict",
                "Extraction will not overwrite existing InfoUnits without a safe refresh strategy", 409)
        parsed_ids = [f.file_id for f in case.files if f.parse_status == ParseStatus.parsed]
        audit["excluded_unparsed_files"] = [
            {"file_id": f.file_id, "parse_status": f.parse_status.value, "error_message": f.error_message}
            for f in case.files if f.parse_status != ParseStatus.parsed]
        if not parsed_ids:
            raise AppError("no_parsed_files", "The case has no successfully parsed files")
        quote_index_events: list[QuoteIndexEvent] = []
        model_input = build_quote_model_input(case, parsed_ids, quote_index_events)
        audit["input_quote_indexes"] = [event.model_dump(mode="json") for event in quote_index_events]
        prompt, repair_prompt = load_prompts()
        run.prompt_version = prompt.version
        config = llm_client.load_config()
        run.model = config.model
        model_input["confirmed_source_kinds"] = {
            f.file_id: f.source_kind.value for f in case.files if f.file_id in parsed_ids}
        write_json(artifact_path(case.case_id, run.input_snapshot_path), llm_client.redact({
            "run_id": run_id, "run_name": trace.run_name, "workflow_id": workflow_id,
            "model_input": model_input, "system_prompt": prompt.content,
            "validation_repair_prompt": repair_prompt.content,
            "prompts": {
                item.name: item.metadata(config.model)
                for item in (prompt, repair_prompt)
            },
            "prompt_version": prompt.version, "schema_version": prompt.schema_version,
            "configuration": config.public_metadata(),
            "case_snapshot_sha256": hashlib.sha256(initial_snapshot).hexdigest()}))
        audit["active_step"] = "extraction"
        audit["model_request_attempts"] = 1
        # Write the attempted step before the call, including on interruption.
        write_json(artifact_path(case.case_id, run.output_snapshot_path), audit)
        schema = ModelExtraction.model_json_schema()
        audit["model_calls"].append(_model_call_record("extraction", prompt, config))
        with trace.generation(
                "Extraction · model call", config=config, prompt=prompt,
                model_input=model_input, schema=schema) as generation:
            reply = await llm_client.call_structured(config, prompt.content, model_input, schema)
            generation.record_reply(reply)
        audit["raw_response"] = reply.response
        audit["model_content"] = reply.content
        with trace.observation(
                "Extraction · parse", as_type="evaluator", input=reply.content) as parse_span:
            payload = parse_extraction_payload(reply.content)
            parse_span.update(output=payload)
        audit["parsed_result"] = payload
        result, merges = await _validate_repair_and_materialize(
            payload, case, config=config, repair_prompt=repair_prompt,
            run=run, audit=audit, trace=trace)
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
        audit["result_status"] = "partial_success" if audit["discarded_count"] else "success"
        _write_audit(case.case_id, run, audit)
        candidate.stage_runs.append(run)
        candidate.updated_at = now_iso()
        save_case(candidate, set_current=False)
        trace.finish_success({
            "status": audit["result_status"], "accepted_count": audit["accepted_count"],
            "discarded_count": audit["discarded_count"], "result": audit["result"]})
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
        audit["result_status"] = "failed"
        audit["exception_type"] = type(exc).__name__
        audit["error_code"] = failure.code
        audit["errors"] = (
            failure.details.get("errors")
            or getattr(exc, "errors", [])
            or [{"item_id": BATCH_ITEM_ID, "field_path": "$",
                 "error_code": failure.code, "message": str(failure)}]
        )
        if isinstance(exc, llm_client.LLMFailure):
            if audit["active_step"] == "extraction_validation_repair":
                audit.setdefault("repair", {})["raw_response"] = exc.response
            else:
                audit["raw_response"] = exc.response
        trace.finish_error(exc, step=audit["active_step"])
        try:
            if not artifact_path(case.case_id, run.input_snapshot_path).exists():
                write_json(artifact_path(case.case_id, run.input_snapshot_path), {
                    "run_id": run_id, "run_name": trace.run_name, "workflow_id": workflow_id,
                    "case_id": case.case_id, "input_preparation_failed": True})
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
