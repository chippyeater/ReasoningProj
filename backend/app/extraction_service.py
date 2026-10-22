"""TXT -> LLM -> validated v0.3 extraction, with an auditable atomic commit."""
from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from app import llm_client
from app.observability import RunTrace
from app.case_service import now_iso
from app.case_store import artifact_path, case_dir, load_case_snapshot, save_case, write_json
from app.errors import AppError
from app.extraction import (
    ExtractionFailure, ModelEvidenceRepair, ModelExtraction,
    apply_evidence_repair, materialize_extraction, parse_evidence_repair,
    parse_model_extraction,
)
from app.quote_units import QuoteIndexEvent, build_quote_model_input
from app.prompt_loader import LoadedPrompt, load_prompt
from app.schemas import CaseData, ExtractionOutput, ParseStatus, PipelineStage, RunStatus, StageRun

PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts/extraction_system_prompt.md"
EVIDENCE_REPAIR_PROMPT_PATH = PROMPT_PATH.with_name("extraction_evidence_repair_prompt.md")


def load_prompts() -> tuple[LoadedPrompt, LoadedPrompt]:
    try:
        prompt = load_prompt(PROMPT_PATH, "extraction")
        repair = load_prompt(EVIDENCE_REPAIR_PROMPT_PATH, "evidence_repair")
    except (OSError, UnicodeError, ValueError) as exc:
        raise AppError(
            "extraction_prompt_unavailable", f"Extraction prompt could not be loaded: {exc}", 500) from exc
    return prompt, repair


def _model_call_record(step: str, prompt: LoadedPrompt,
                       config: llm_client.LLMConfig) -> dict[str, str | None]:
    return {"step": step, **prompt.metadata(config.model)}


def evidence_repair_enabled() -> bool:
    raw = llm_client.configuration_values().get("EXTRACTION_EVIDENCE_REPAIR_ENABLED", "false").strip().lower()
    if raw in {"false", "0", "no", "off"}:
        return False
    if raw in {"true", "1", "yes", "on"}:
        return True
    raise AppError("llm_configuration_invalid",
                   "EXTRACTION_EVIDENCE_REPAIR_ENABLED must be a boolean", 503)


def _snapshot_bytes(case_id: str) -> bytes:
    return (case_dir(case_id) / "case.json").read_bytes()


def _write_audit(case_id: str, run: StageRun, audit: dict) -> None:
    write_json(artifact_path(case_id, run.output_snapshot_path), llm_client.redact(audit))
    # Standalone StageRun remains discoverable even if case artifacts are broken.
    write_json(artifact_path(case_id, f"runs/{run.id}.stage.json"), run.model_dump(mode="json"))


async def _materialize_with_optional_repair(
        draft: ModelExtraction, case: CaseData, *, model_input: dict,
        config: llm_client.LLMConfig, repair_prompt: LoadedPrompt, repair_enabled: bool,
        run: StageRun, audit: dict, trace: RunTrace) -> tuple[ExtractionOutput, list[dict]]:
    diagnostics: dict = {}
    try:
        with trace.observation(
                "Extraction · source validation", as_type="guardrail",
                input=draft.model_dump(mode="json")) as validation_span:
            try:
                result, merges = materialize_extraction(draft, case, diagnostics)
            except ExtractionFailure as exc:
                validation_span.update(output={
                    "valid": False, "diagnostics": exc.diagnostics or diagnostics,
                    "errors": exc.errors,
                }, level="ERROR", status_message=exc.code)
                raise
            validation_span.update(output={"valid": True, "diagnostics": diagnostics, "merges": merges})
    except ExtractionFailure as exc:
        audit["source_validation"] = exc.diagnostics or diagnostics
        repairable_codes = {"invalid_evidence_id", "quote_unit_not_evidence"}
        repairable = (exc.code == "extraction_source_invalid" and exc.errors
                      and all(error.get("code") in repairable_codes for error in exc.errors))
        if not repair_enabled or audit["evidence_repair_attempts"] >= 1 or not repairable:
            raise
        failed_ids = {error["item_id"] for error in exc.errors if error.get("item_id")}
        invalid_nodes = [
            {"local_id": unit.local_id, "type": unit.type,
             "subtype": unit.subtype.value if hasattr(unit.subtype, "value") else unit.subtype,
             "title": unit.title, "content": unit.content, "speaker": unit.speaker,
             "side": unit.side, "actor_local_id": unit.actor_local_id,
             "target_local_id": unit.target_local_id, "related_issue": unit.related_issue,
             "evidence_ids": list(unit.evidence_ids),
             "errors": [error for error in exc.errors if error.get("item_id") == unit.local_id]}
            for unit in draft.info_units if unit.local_id in failed_ids
        ]
        repair_input = {
            "case_title": model_input["case_title"], "files": model_input["files"],
            "invalid_nodes": invalid_nodes,
        }
        repair_path = f"runs/{run.id}.evidence-repair.input.json"
        audit["active_step"] = "evidence_repair"
        audit["evidence_repair_attempts"] = 1
        audit["model_request_attempts"] += 1
        audit["evidence_repair"] = {"input_path": repair_path, "failed_local_ids": sorted(failed_ids)}
        write_json(artifact_path(case.case_id, repair_path), llm_client.redact(repair_input))
        write_json(artifact_path(case.case_id, run.output_snapshot_path), llm_client.redact(audit))
        repair_schema = ModelEvidenceRepair.model_json_schema()
        audit["model_calls"].append(_model_call_record(
            "evidence_repair", repair_prompt, config))
        with trace.generation(
                "Evidence Repair · model call", config=config,
                prompt=repair_prompt, model_input=repair_input, schema=repair_schema) as generation:
            reply = await llm_client.call_structured(
                config, repair_prompt.content, repair_input, repair_schema)
            generation.record_reply(reply)
        audit["evidence_repair"]["raw_response"] = reply.response
        audit["evidence_repair"]["content"] = reply.content
        with trace.observation(
                "Evidence Repair · parse", as_type="evaluator",
                input=reply.content) as parse_span:
            repair = parse_evidence_repair(reply.content)
            parse_span.update(output=repair.model_dump(mode="json"))
        repaired, changes = apply_evidence_repair(draft, repair, failed_ids)
        audit["evidence_repair"]["changes"] = changes
        audit["evidence_repair"]["repaired_result"] = repaired.model_dump(mode="json")
        repaired_diagnostics: dict = {}
        with trace.observation(
                "Evidence Repair · source validation", as_type="guardrail",
                input=repaired.model_dump(mode="json")) as repaired_validation_span:
            try:
                result, merges = materialize_extraction(repaired, case, repaired_diagnostics)
            except ExtractionFailure as repaired_exc:
                audit["source_validation_after_repair"] = repaired_exc.diagnostics or repaired_diagnostics
                repaired_validation_span.update(output={
                    "valid": False, "diagnostics": repaired_exc.diagnostics or repaired_diagnostics,
                    "errors": repaired_exc.errors,
                }, level="ERROR", status_message=repaired_exc.code)
                raise
            repaired_validation_span.update(output={
                "valid": True, "diagnostics": repaired_diagnostics, "merges": merges,
            })
        audit["source_validation_after_repair"] = repaired_diagnostics
        return result, merges
    audit["source_validation"] = diagnostics
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
                   "status": "started", "raw_response": None,
                   "model_content": None, "parsed_result": None, "result": None, "errors": [],
                   "active_step": "preparation", "model_request_attempts": 0,
                   "evidence_repair_attempts": 0, "model_calls": []}
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
        quote_index_events: list[QuoteIndexEvent] = []
        model_input = build_quote_model_input(case, parsed_ids, quote_index_events)
        audit["input_quote_indexes"] = [event.model_dump(mode="json") for event in quote_index_events]
        prompt, repair_prompt = load_prompts()
        run.prompt_version = prompt.version
        config = llm_client.load_config()
        repair_enabled = evidence_repair_enabled()
        audit["evidence_repair_enabled"] = repair_enabled
        run.model = config.model
        model_input["confirmed_source_kinds"] = {
            f.file_id: f.source_kind.value for f in case.files if f.file_id in parsed_ids}
        write_json(artifact_path(case.case_id, run.input_snapshot_path), llm_client.redact({
            "run_id": run_id, "run_name": trace.run_name, "workflow_id": workflow_id,
            "model_input": model_input, "system_prompt": prompt.content,
            "evidence_repair_prompt": repair_prompt.content,
            "evidence_repair_enabled": repair_enabled,
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
            draft = parse_model_extraction(reply.content)
            parse_span.update(output=draft.model_dump(mode="json"))
        audit["parsed_result"] = draft.model_dump(mode="json")
        result, merges = await _materialize_with_optional_repair(
            draft, case, model_input=model_input, config=config,
            repair_prompt=repair_prompt, repair_enabled=repair_enabled, run=run, audit=audit, trace=trace)
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
        trace.finish_success({"status": "success", "result": audit["result"]})
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
            if audit["active_step"] == "evidence_repair":
                audit.setdefault("evidence_repair", {})["raw_response"] = exc.response
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
