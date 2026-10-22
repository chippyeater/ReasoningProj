"""One-shot InfoUnit proposals with explicit, stale-safe CRUD confirmation."""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from app import llm_client
from app.api_models import (
    ConfirmInfoUnitAgentProposalInput,
    CreateInfoUnitInput,
    InfoUnitAgentAction,
    InfoUnitAgentChange,
    InfoUnitAgentEvidence,
    InfoUnitAgentProposalResponse,
    InfoUnitAgentRequest,
    InfoUnitMutationAction,
    ModelInfoUnitAgentProposal,
    UpdateInfoUnitInput,
)
from app.case_service import (
    create_info_unit,
    evidence_ids_for_info_unit,
    list_quote_units,
    now_iso,
    prepare_info_unit_creation,
    prepare_info_unit_update,
    update_info_unit,
)
from app.case_store import artifact_path, load_case_snapshot, write_json
from app.errors import AppError
from app.observability import RunTrace
from app.prompt_loader import LoadedPrompt, load_prompt
from app.schemas import CaseData


PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts/info_unit_agent_prompt.md"
LOGGER = logging.getLogger(__name__)
MUTABLE_FIELDS = (
    "type", "subtype", "title", "content", "evidence_ids", "speaker", "side",
    "actor", "target", "related_issue",
)


def case_content_hash(case: CaseData) -> str:
    """Hash business state while ignoring canvas-only state and its timestamp churn."""
    payload = case.model_dump(mode="json", exclude={"workspace_state", "updated_at"})
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _prompt() -> LoadedPrompt:
    try:
        return load_prompt(PROMPT_PATH, "info_unit_agent")
    except (OSError, UnicodeError, ValueError) as exc:
        raise AppError(
            "info_unit_agent_prompt_unavailable",
            f"InfoUnit Agent prompt could not be loaded: {exc}", 500) from exc


def _write_agent_audit(case_id: str, path: str, payload: dict) -> None:
    try:
        write_json(artifact_path(case_id, path), llm_client.redact(payload))
    except (OSError, AppError) as exc:
        LOGGER.warning("InfoUnit Agent local audit write failed: %s", type(exc).__name__)


def _strict_model_proposal(raw: str) -> ModelInfoUnitAgentProposal:
    def unique_keys(pairs: list[tuple[str, object]]) -> dict:
        result: dict = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(
            raw, object_pairs_hook=unique_keys,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"Non-JSON constant: {value}")),
        )
    except (ValueError, TypeError) as exc:
        raise AppError(
            "info_unit_agent_json_invalid", "Model proposal is not strict JSON",
            details={"errors": [{"message": str(exc)}]},
        ) from exc
    try:
        return ModelInfoUnitAgentProposal.model_validate_json(
            json.dumps(value, ensure_ascii=False), strict=True)
    except ValidationError as exc:
        raise AppError(
            "info_unit_agent_schema_invalid", "Model proposal violates the InfoUnit proposal contract",
            details={"errors": exc.errors(include_url=False, include_input=False, include_context=False)},
        ) from exc


def _model_context(case: CaseData) -> dict:
    files = {file.file_id: file for file in case.files}
    quotes = list_quote_units(case)
    return {
        "case": {
            "case_id": case.case_id,
            "case_title": case.case_title,
            "core_issue": case.core_issue,
            "issue_set": case.issue_set,
            "info_units": [
                {
                    **unit.model_dump(mode="json", exclude={"source_refs", "created_at"}),
                    "evidence_ids": evidence_ids_for_info_unit(case, unit),
                }
                for unit in case.info_units
            ],
            "quote_units": [
                {
                    **unit.model_dump(mode="json"),
                    "filename": files[unit.file_id].filename,
                    "source_kind": files[unit.file_id].source_kind.value,
                }
                for unit in quotes
                if unit.role != "group_boundary"
            ],
        },
    }


def _create_payload(proposed) -> CreateInfoUnitInput:
    return CreateInfoUnitInput.model_validate(proposed.model_dump(mode="json"))


def _update_payload(proposed) -> UpdateInfoUnitInput:
    return UpdateInfoUnitInput.model_validate(proposed.model_dump(mode="json"))


def _validate_operation(case: CaseData, action: InfoUnitMutationAction,
                        target_info_unit_id: str | None, proposed) -> None:
    if action == InfoUnitMutationAction.create:
        if target_info_unit_id is not None:
            raise AppError("invalid_info_unit_agent_target", "Create proposal must not specify a target InfoUnit")
        prepare_info_unit_creation(case, _create_payload(proposed))
        return
    if target_info_unit_id is None:
        raise AppError("invalid_info_unit_agent_target", "Update proposal requires a target InfoUnit")
    prepare_info_unit_update(case, target_info_unit_id, _update_payload(proposed))
    current = next(unit for unit in case.info_units if unit.id == target_info_unit_id)
    old_values = current.model_dump(mode="json")
    old_values["evidence_ids"] = evidence_ids_for_info_unit(case, current)
    proposed_values = proposed.model_dump(mode="json")
    if all(old_values[field] == proposed_values[field] for field in MUTABLE_FIELDS):
        raise AppError("info_unit_agent_no_changes", "Update proposal does not change the target InfoUnit")


def _proposal_response(case: CaseData, action: InfoUnitMutationAction,
                       target_info_unit_id: str | None, proposed,
                       base_case_hash: str) -> InfoUnitAgentProposalResponse:
    proposed_values = proposed.model_dump(mode="json")
    old_values = {field: None for field in MUTABLE_FIELDS}
    if action == InfoUnitMutationAction.update:
        current = next(unit for unit in case.info_units if unit.id == target_info_unit_id)
        old_values.update(current.model_dump(mode="json"))
        old_values["evidence_ids"] = evidence_ids_for_info_unit(case, current)
    changes = [
        InfoUnitAgentChange(field=field, old_value=old_values[field], new_value=proposed_values[field])
        for field in MUTABLE_FIELDS
        if old_values[field] != proposed_values[field]
    ]
    unchanged = [field for field in MUTABLE_FIELDS if old_values[field] == proposed_values[field]]
    quote_lookup = {quote.quote_id: quote for quote in list_quote_units(case)}
    file_lookup = {file.file_id: file for file in case.files}
    evidence = [
        InfoUnitAgentEvidence(
            quote_id=quote_id, file_id=quote_lookup[quote_id].file_id,
            filename=file_lookup[quote_lookup[quote_id].file_id].filename,
            text=quote_lookup[quote_id].text, role=quote_lookup[quote_id].role,
            context_quote_ids=quote_lookup[quote_id].context_quote_ids,
        )
        for quote_id in proposed.evidence_ids
    ]
    return InfoUnitAgentProposalResponse(
        action=action, target_info_unit_id=target_info_unit_id, proposed=proposed,
        changes=changes, unchanged_fields=unchanged, evidence=evidence,
        base_case_hash=base_case_hash, message=None,
    )


async def generate_info_unit_proposal(case: CaseData,
                                      request: InfoUnitAgentRequest) -> InfoUnitAgentProposalResponse:
    known_ids = {unit.id for unit in case.info_units}
    unknown_context = set(request.selected_info_unit_ids) - known_ids
    if unknown_context:
        raise AppError(
            "invalid_info_unit_agent_context",
            f"Unknown current-case selected InfoUnit IDs: {sorted(unknown_context)}",
        )
    base_hash = case_content_hash(case)
    model_input = {
        **_model_context(case),
        "request": request.model_dump(mode="json", exclude={"workflow_id"}),
    }
    workflow_id = request.workflow_id or f"workflow-{uuid4().hex}"
    run_id = f"agent-run-{uuid4().hex}"
    input_path = f"runs/{run_id}.input.json"
    output_path = f"runs/{run_id}.output.json"
    trace = RunTrace(
        case_id=case.case_id, case_title=case.case_title, stage="InfoUnit Agent",
        run_id=run_id, workflow_id=workflow_id, created_at=now_iso(),
        initial_input=model_input,
    )
    audit: dict = {
        "run_id": run_id, "run_name": trace.run_name, "workflow_id": workflow_id,
        "stage": "info_unit_agent", "status": "started", "model_calls": [],
        "raw_response": None, "model_content": None, "parsed_result": None,
        "validation_result": None, "result": None,
    }
    try:
        config = llm_client.load_config()
        prompt = _prompt()
        schema = ModelInfoUnitAgentProposal.model_json_schema()
        prompt_metadata = prompt.metadata(config.model)
        audit["model_calls"].append({"step": "info_unit_agent", **prompt_metadata})
        _write_agent_audit(case.case_id, input_path, {
            "run_id": run_id, "run_name": trace.run_name, "workflow_id": workflow_id,
            "model_input": model_input, "system_prompt": prompt.content,
            "schema": schema, **prompt_metadata, "configuration": config.public_metadata(),
        })
        _write_agent_audit(case.case_id, output_path, audit)
        with trace.generation(
                "InfoUnit Agent · model call", config=config, prompt=prompt,
                model_input=model_input, schema=schema) as generation:
            reply = await llm_client.call_structured(config, prompt.content, model_input, schema)
            generation.record_reply(reply)
        audit["raw_response"] = reply.response
        audit["model_content"] = reply.content
        with trace.observation(
                "InfoUnit Agent · parse", as_type="evaluator", input=reply.content) as parse_span:
            result = _strict_model_proposal(reply.content)
            parse_span.update(output=result.model_dump(mode="json"))
        audit["parsed_result"] = result.model_dump(mode="json")
        latest = load_case_snapshot(case.case_id)
        if latest is None or case_content_hash(latest) != base_hash:
            raise AppError(
                "case_changed_during_info_unit_proposal",
                "Case changed while the proposal was being generated; generate a new proposal",
                409,
            )
        if result.action in {InfoUnitAgentAction.clarify, InfoUnitAgentAction.unsupported}:
            response = InfoUnitAgentProposalResponse(
                action=result.action, target_info_unit_id=None, proposed=None,
                changes=[], unchanged_fields=[], evidence=[], base_case_hash=base_hash,
                message=result.message,
            )
            audit["validation_result"] = {"valid": True, "action": result.action.value}
            audit["result"] = response.model_dump(mode="json")
            audit["status"] = "success"
            _write_agent_audit(case.case_id, output_path, audit)
            trace.finish_success(response.model_dump(mode="json"))
            return response
        if result.target_info_unit_id is not None and result.target_info_unit_id not in known_ids:
            raise AppError(
                "info_unit_not_found", f"InfoUnit not found: {result.target_info_unit_id}", 404)
        action = InfoUnitMutationAction(result.action.value)
        with trace.observation(
                "InfoUnit Agent · validate proposal", as_type="guardrail",
                input=result.model_dump(mode="json")) as validation_span:
            _validate_operation(latest, action, result.target_info_unit_id, result.proposed)
            response = _proposal_response(
                latest, action, result.target_info_unit_id, result.proposed, base_hash)
            validation_span.update(output={"valid": True, "action": action.value})
        audit["validation_result"] = {"valid": True, "action": action.value}
        audit["result"] = response.model_dump(mode="json")
        audit["status"] = "success"
        _write_agent_audit(case.case_id, output_path, audit)
        trace.finish_success(response.model_dump(mode="json"))
        return response
    except Exception as exc:
        audit["status"] = "failed"
        audit["error"] = trace.error_payload(exc, step=trace.active_step)
        if isinstance(exc, llm_client.LLMFailure):
            audit["raw_response"] = exc.response
        _write_agent_audit(case.case_id, output_path, audit)
        trace.finish_error(exc, step=trace.active_step)
        raise


def confirm_info_unit_proposal(case: CaseData,
                               request: ConfirmInfoUnitAgentProposalInput) -> CaseData:
    if case_content_hash(case) != request.base_case_hash:
        raise AppError(
            "stale_info_unit_proposal",
            "Case changed after this proposal was generated; generate a new proposal before confirming",
            409,
        )
    _validate_operation(case, request.action, request.target_info_unit_id, request.proposed)
    if request.action == InfoUnitMutationAction.create:
        return create_info_unit(case, _create_payload(request.proposed))
    return update_info_unit(case, request.target_info_unit_id or "", _update_payload(request.proposed))
