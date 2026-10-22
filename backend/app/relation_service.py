"""Incremental Relation generation and single-relation persistence."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import ConfigDict, ValidationError, field_validator, model_validator

from app import llm_client
from app.observability import RunTrace
from app.api_models import CreateRelationInput, RelationRunResponse, UpdateRelationInput
from app.case_service import now_iso
from app.case_store import artifact_path, case_dir, load_case_snapshot, save_case, write_json
from app.errors import AppError
from app.schemas import (
    CaseData, ContractModel, PipelineStage, Relation, RelationInput, RelationOutput, RelationType,
    RunStatus, StageRun, Text,
)
from app.prompt_loader import LoadedPrompt, load_prompt
from app.validation import relation_key, validate_relation_candidates
from app.validation_repair import (
    BATCH_ITEM_ID, item_errors, locatable_items, merge_repair_results, pydantic_issues,
    repair_items, require_exact_fields, strict_json_object, validate_resolution,
)

PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts/relation_system_prompt.md"
REPAIR_PROMPT_PATH = PROMPT_PATH.with_name("relation_validation_repair_prompt.md")


def _relation_key(relation: Relation) -> tuple[str, str, RelationType]:
    return relation_key(relation)


def _new_relation_id(existing_ids: set[str]) -> str:
    relation_id = f"relation-{uuid4().hex}"
    while relation_id in existing_ids:
        relation_id = f"relation-{uuid4().hex}"
    existing_ids.add(relation_id)
    return relation_id


def _validated_candidate(case: CaseData, relations: list[Relation]) -> CaseData:
    candidate = case.model_copy(deep=True)
    candidate.relations = relations
    candidate.updated_at = now_iso()
    try:
        return CaseData.model_validate(candidate.model_dump(mode="json"))
    except ValidationError as exc:
        raise AppError(
            "invalid_relation_change", "Relation change violates the current case contract", 422,
            details={"errors": exc.errors(include_url=False, include_input=False, include_context=False)},
        ) from exc


def _ensure_not_duplicate(relation: Relation, others: list[Relation]) -> None:
    if _relation_key(relation) in {_relation_key(item) for item in others}:
        raise AppError("duplicate_relation", "An equivalent Relation already exists", 409)


def create_relation(case: CaseData, payload: CreateRelationInput) -> CaseData:
    ids = {item.id for item in case.relations}
    try:
        relation = Relation(id=_new_relation_id(ids), **payload.model_dump(mode="json"))
    except ValidationError as exc:
        raise AppError("invalid_relation", "Relation violates the current schema", 422) from exc
    _ensure_not_duplicate(relation, case.relations)
    candidate = _validated_candidate(case, [*case.relations, relation])
    save_case(candidate)
    return candidate


def update_relation(case: CaseData, relation_id: str, payload: UpdateRelationInput) -> CaseData:
    index = next((i for i, item in enumerate(case.relations) if item.id == relation_id), None)
    if index is None:
        raise AppError("relation_not_found", f"Relation not found: {relation_id}", 404)
    values = case.relations[index].model_dump(mode="json")
    values.update(payload.model_dump(mode="json", exclude_unset=True))
    values["id"] = relation_id
    try:
        relation = Relation.model_validate(values)
    except ValidationError as exc:
        raise AppError("invalid_relation", "Relation violates the current schema", 422) from exc
    _ensure_not_duplicate(relation, [item for item in case.relations if item.id != relation_id])
    relations = list(case.relations)
    relations[index] = relation
    candidate = _validated_candidate(case, relations)
    save_case(candidate)
    return candidate


def delete_relation(case: CaseData, relation_id: str) -> CaseData:
    index = next((i for i, item in enumerate(case.relations) if item.id == relation_id), None)
    if index is None:
        raise AppError("relation_not_found", f"Relation not found: {relation_id}", 404)
    # Current ReasoningView payloads reference InfoUnits, not Relation IDs. If that
    # contract grows a relation reference, CaseData validation must reject deletion.
    relations = list(case.relations)
    relations.pop(index)
    candidate = _validated_candidate(case, relations)
    save_case(candidate)
    return candidate


class RelationValidationRepair(ContractModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    repairs: list[Relation]
    discarded_ids: list[Text]

    @field_validator("repairs", "discarded_ids")
    @classmethod
    def unique_ids(cls, value):
        ids = [item.id for item in value] if value and hasattr(value[0], "id") else value
        if len(ids) != len(set(ids)):
            raise ValueError("repair IDs must be unique")
        return value

    @model_validator(mode="after")
    def disjoint_results(self) -> RelationValidationRepair:
        if {item.id for item in self.repairs} & set(self.discarded_ids):
            raise ValueError("repairs and discarded_ids must be disjoint")
        return self


@dataclass
class RelationCandidateReport:
    ordered_ids: list[str]
    raw_by_id: dict[str, dict[str, Any]]
    models_by_id: dict[str, Relation]
    accepted: list[Relation]
    errors: list[dict[str, str]]

    @property
    def failed_ids(self) -> set[str]:
        return {error["item_id"] for error in self.errors if error["item_id"] != BATCH_ITEM_ID}

    def audit_payload(self) -> dict[str, Any]:
        grouped = item_errors(self.errors)
        accepted_ids = {relation.id for relation in self.accepted}
        return {
            "accepted_ids": [item_id for item_id in self.ordered_ids if item_id in accepted_ids],
            "failed_ids": [item_id for item_id in self.ordered_ids if item_id in self.failed_ids],
            "errors": self.errors,
            "candidates": [
                {
                    "id": item_id,
                    "status": "accepted" if item_id in accepted_ids else "failed",
                    "errors": grouped.get(item_id, []),
                }
                for item_id in self.ordered_ids
            ],
        }


def parse_relation_payload(raw: str) -> dict[str, Any]:
    value = strict_json_object(raw, code="relation_json_invalid", label="Relation output")
    require_exact_fields(value, {"relations"}, code="relation_schema_invalid", label="Relation output")
    locatable_items(
        value["relations"], list_field="relations", id_field="id",
        code="relation_schema_invalid", label="Relation output")
    return value


def validate_relation_payload(
        payload: dict[str, Any], context: RelationInput,
        existing_relations: list[Relation],
        ) -> RelationCandidateReport:
    require_exact_fields(payload, {"relations"}, code="relation_schema_invalid", label="Relation output")
    located = locatable_items(
        payload["relations"], list_field="relations", id_field="id",
        code="relation_schema_invalid", label="Relation output")
    models_by_id: dict[str, Relation] = {}
    errors: list[dict[str, str]] = []
    for item_id, raw_item in located:
        try:
            models_by_id[item_id] = Relation.model_validate_json(
                json.dumps(raw_item, ensure_ascii=False), strict=True)
        except ValidationError as exc:
            errors.extend(pydantic_issues(item_id, exc))
    schema_valid = [models_by_id[item_id] for item_id, _ in located if item_id in models_by_id]
    accepted, business_errors = validate_relation_candidates(
        schema_valid, context, existing_relations)
    errors.extend(business_errors)
    return RelationCandidateReport(
        ordered_ids=[item_id for item_id, _ in located],
        raw_by_id={item_id: raw_item for item_id, raw_item in located},
        models_by_id=models_by_id,
        accepted=accepted,
        errors=errors,
    )


def _load_prompts() -> tuple[LoadedPrompt, LoadedPrompt]:
    try:
        return (
            load_prompt(PROMPT_PATH, "relation"),
            load_prompt(REPAIR_PROMPT_PATH, "relation_validation_repair"),
        )
    except (OSError, UnicodeError, ValueError) as exc:
        raise AppError(
            "relation_prompt_unavailable", f"Relation prompt could not be loaded: {exc}", 500) from exc


def _relation_summary(relation: Relation) -> dict[str, Any]:
    return {
        "id": relation.id,
        "source_id": relation.source_id,
        "target_id": relation.target_id,
        "relation_type": relation.relation_type.value,
    }


def _snapshot_bytes(case_id: str) -> bytes:
    return (case_dir(case_id) / "case.json").read_bytes()


def _write_audit(case_id: str, run: StageRun, audit: dict) -> None:
    write_json(artifact_path(case_id, run.output_snapshot_path), llm_client.redact(audit))
    write_json(artifact_path(case_id, f"runs/{run.id}.stage.json"), run.model_dump(mode="json"))


async def run_relation(case: CaseData, selected_info_unit_ids: list[str] | None = None,
                       *, workflow_id: str | None = None) -> RelationRunResponse:
    run_id = f"run-{uuid4().hex}"
    workflow_id = workflow_id or f"workflow-{uuid4().hex}"
    run = StageRun(
        id=run_id, stage=PipelineStage.relation, status=RunStatus.failed, model=None,
        prompt_version=None, input_snapshot_path=f"runs/{run_id}.input.json",
        output_snapshot_path=f"runs/{run_id}.output.json", error_message=None, created_at=now_iso(),
    )
    trace = RunTrace(
        case_id=case.case_id, case_title=case.case_title, stage="Relation",
        run_id=run_id, workflow_id=workflow_id, created_at=run.created_at,
        initial_input={"case_id": case.case_id, "selected_info_unit_ids": selected_info_unit_ids},
    )
    scope_ids = selected_info_unit_ids or [unit.id for unit in case.info_units]
    audit: dict = {
        "run_id": run_id, "run_name": trace.run_name, "workflow_id": workflow_id,
        "status": "started", "result_status": None, "scope_info_unit_ids": scope_ids,
        "raw_response": None, "model_content": None, "initial_validation": None,
        "repair": None, "revalidation": None, "added_relations": [], "discarded_relations": [],
        "accepted_count": 0, "discarded_count": 0, "errors": [],
        "model_request_attempts": 0, "model_calls": [], "active_step": "preparation",
    }
    initial_snapshot = _snapshot_bytes(case.case_id)
    try:
        if selected_info_unit_ids is not None and len(selected_info_unit_ids) < 2:
            raise AppError("relation_scope_too_small", "Select at least two InfoUnits", 422)
        lookup = {unit.id: unit for unit in case.info_units}
        unknown = [unit_id for unit_id in scope_ids if unit_id not in lookup]
        if unknown:
            raise AppError("invalid_relation_scope", "Relation scope contains unknown InfoUnit IDs", 422,
                           details={"unknown_info_unit_ids": unknown})
        if len(scope_ids) < 2:
            if selected_info_unit_ids is not None:
                raise AppError("relation_scope_too_small", "At least two InfoUnits are required", 422)
            write_json(artifact_path(case.case_id, run.input_snapshot_path), {
                "run_id": run_id, "run_name": trace.run_name, "workflow_id": workflow_id,
                "case_id": case.case_id, "model_skipped": "fewer_than_two_info_units",
            })
            run.status = RunStatus.success
            audit["status"] = "success"
            audit["result_status"] = "empty"
            audit["model_skipped"] = "fewer_than_two_info_units"
            _write_audit(case.case_id, run, audit)
            candidate = case.model_copy(deep=True)
            candidate.stage_runs.append(run)
            candidate.updated_at = now_iso()
            save_case(candidate, set_current=False)
            trace.finish_success({"status": "success", "added_relations": [], "model_skipped": True})
            return RelationRunResponse(status="empty", added_relations=[], discarded_count=0)

        scope_set = set(scope_ids)
        context = RelationInput(core_issue=case.core_issue, issue_set=case.issue_set,
                                info_units=[lookup[unit_id] for unit_id in scope_ids])
        existing_in_scope = [
            relation for relation in case.relations
            if relation.source_id in scope_set and relation.target_id in scope_set
        ]
        model_input = context.model_dump(mode="json") | {
            "scope": "selected" if selected_info_unit_ids is not None else "all",
            "existing_relations": [item.model_dump(mode="json") for item in existing_in_scope],
        }
        prompt, repair_prompt = _load_prompts()
        run.prompt_version = prompt.version
        config = llm_client.load_config()
        run.model = config.model
        write_json(artifact_path(case.case_id, run.input_snapshot_path), llm_client.redact({
            "run_id": run_id, "run_name": trace.run_name, "workflow_id": workflow_id,
            "model_input": model_input, "system_prompt": prompt.content,
            "repair_prompt": repair_prompt.content,
            "prompts": {
                item.name: item.metadata(config.model) for item in (prompt, repair_prompt)
            },
            **prompt.metadata(config.model), "configuration": config.public_metadata(),
            "case_snapshot_sha256": hashlib.sha256(initial_snapshot).hexdigest(),
        }))
        _write_audit(case.case_id, run, audit)
        schema = RelationOutput.model_json_schema()
        audit["model_calls"].append({"step": "relation", **prompt.metadata(config.model)})
        audit["model_request_attempts"] = 1
        audit["active_step"] = "relation"
        with trace.generation(
                "Relation · model call", config=config, prompt=prompt,
                model_input=model_input, schema=schema) as generation:
            reply = await llm_client.call_structured(config, prompt.content, model_input, schema)
            generation.record_reply(reply)
        audit["raw_response"] = reply.response
        audit["model_content"] = reply.content
        with trace.observation(
                "Relation · parse", as_type="evaluator", input=reply.content) as parse_span:
            payload = parse_relation_payload(reply.content)
            parse_span.update(output=payload)
        with trace.observation(
                "Relation · validate", as_type="guardrail",
                input=payload) as validation_span:
            initial_report = validate_relation_payload(payload, context, case.relations)
            validation_payload = initial_report.audit_payload()
            validation_span.update(
                output=validation_payload,
                level="ERROR" if initial_report.errors else None,
                status_message="relation_validation_failed" if initial_report.errors else None,
            )
        audit["initial_validation"] = validation_payload
        final_report = initial_report
        explicitly_discarded: set[str] = set()

        if initial_report.errors:
            failed_ids = initial_report.failed_ids
            repair_input = {
                "failed_relations": repair_items(
                    initial_report.ordered_ids, initial_report.raw_by_id,
                    initial_report.errors),
                "info_unit_summaries": [
                    {
                        "id": unit.id, "type": unit.type.value,
                        "subtype": unit.subtype.value if unit.subtype is not None else None,
                        "title": unit.title, "content": unit.content,
                        "actor": unit.actor, "target": unit.target,
                        "related_issue": unit.related_issue,
                    }
                    for unit in context.info_units
                ],
                "accepted_relations": [_relation_summary(item) for item in initial_report.accepted],
                "existing_relations": [_relation_summary(item) for item in existing_in_scope],
                "scope": {
                    "kind": "selected" if selected_info_unit_ids is not None else "all",
                    "allowed_info_unit_ids": scope_ids,
                },
                "constraints": {
                    "allowed_relation_types": [item.value for item in RelationType],
                    "contradict_is_symmetric_for_deduplication": True,
                    "support_target_must_not_be_entity": True,
                    "contradict_endpoints_must_not_be_entity": True,
                },
            }
            repair_path = f"runs/{run.id}.validation-repair.input.json"
            write_json(artifact_path(case.case_id, repair_path), llm_client.redact(repair_input))
            audit["active_step"] = "relation_validation_repair"
            audit["model_request_attempts"] = 2
            audit["repair"] = {
                "input_path": repair_path,
                "failed_ids": [item_id for item_id in initial_report.ordered_ids if item_id in failed_ids],
                "raw_response": None,
                "content": None,
                "changes": [],
            }
            audit["model_calls"].append({
                "step": "relation_validation_repair", **repair_prompt.metadata(config.model)})
            _write_audit(case.case_id, run, audit)
            repair_schema = RelationValidationRepair.model_json_schema()
            with trace.generation(
                    "Relation Validation Repair · model call", config=config, prompt=repair_prompt,
                    model_input=repair_input, schema=repair_schema) as generation:
                repair_reply = await llm_client.call_structured(
                    config, repair_prompt.content, repair_input, repair_schema)
                generation.record_reply(repair_reply)
            audit["repair"]["raw_response"] = repair_reply.response
            audit["repair"]["content"] = repair_reply.content
            with trace.observation(
                    "Relation Validation Repair · parse", as_type="evaluator",
                    input=repair_reply.content) as repair_parse_span:
                repair_payload = strict_json_object(
                    repair_reply.content, code="relation_repair_json_invalid",
                    label="Relation repair output")
                require_exact_fields(
                    repair_payload, {"repairs", "discarded_ids"},
                    code="relation_repair_schema_invalid", label="Relation repair output")
                repaired_located = locatable_items(
                    repair_payload["repairs"], list_field="repairs", id_field="id",
                    code="relation_repair_schema_invalid", label="Relation repair output")
                discarded_value = repair_payload["discarded_ids"]
                if (not isinstance(discarded_value, list)
                        or any(not isinstance(item, str) or not item.strip() for item in discarded_value)
                        or len(discarded_value) != len(set(discarded_value))):
                    raise AppError(
                        "relation_repair_schema_invalid",
                        "discarded_ids must be a unique list of nonempty strings", 422)
                repaired_raw = {item_id: raw_item for item_id, raw_item in repaired_located}
                explicitly_discarded = set(discarded_value)
                validate_resolution(
                    failed_ids, set(repaired_raw), explicitly_discarded,
                    code="relation_repair_schema_invalid", repaired_field="repairs",
                    rejected_field="discarded_ids")
                repair_parse_span.update(output=repair_payload)
            combined_items, changes = merge_repair_results(
                initial_report.ordered_ids, initial_report.raw_by_id, failed_ids,
                repaired_raw, explicitly_discarded)
            audit["repair"]["changes"] = changes
            with trace.observation(
                    "Relation Validation Repair · resolution", as_type="evaluator",
                    input=repair_payload) as resolution_span:
                resolution_span.update(output={
                    "changes": changes,
                    "discarded_ids": sorted(explicitly_discarded),
                })
            with trace.observation(
                    "Relation Validation Repair · revalidate", as_type="guardrail",
                    input={"relations": combined_items}) as revalidation_span:
                final_report = validate_relation_payload(
                    {"relations": combined_items}, context, case.relations)
                revalidation_payload = final_report.audit_payload()
                revalidation_span.update(
                    output=revalidation_payload,
                    level="ERROR" if final_report.errors else None,
                    status_message="relation_revalidation_has_rejections" if final_report.errors else None,
                )
            audit["revalidation"] = revalidation_payload

        rejected_ids = explicitly_discarded | final_report.failed_ids
        rejection_errors = [*initial_report.errors, *final_report.errors]
        audit["discarded_relations"] = [
            {
                "id": item_id,
                "original": initial_report.raw_by_id.get(item_id),
                "errors": [error for error in rejection_errors if error["item_id"] == item_id],
                "explicitly_discarded": item_id in explicitly_discarded,
            }
            for item_id in initial_report.ordered_ids if item_id in rejected_ids
        ]
        generated: list[Relation] = []
        ids = {item.id for item in case.relations}
        for proposed in final_report.accepted:
            generated.append(proposed.model_copy(update={"id": _new_relation_id(ids)}))

        if _snapshot_bytes(case.case_id) != initial_snapshot:
            raise AppError("case_changed_during_relation", "Case changed while the model was running; Relations were not committed", 409)
        candidate = _validated_candidate(case, [*case.relations, *generated])
        run.status = RunStatus.success
        audit["status"] = "success"
        audit["result_status"] = (
            "empty" if not initial_report.ordered_ids
            else "partial_success" if rejected_ids
            else "success"
        )
        audit["accepted_count"] = len(generated)
        audit["discarded_count"] = len(rejected_ids)
        audit["added_relations"] = [item.model_dump(mode="json") for item in generated]
        _write_audit(case.case_id, run, audit)
        candidate.stage_runs.append(run)
        if _snapshot_bytes(case.case_id) != initial_snapshot:
            raise AppError("case_changed_during_relation", "Case changed while the model was running; Relations were not committed", 409)
        save_case(candidate, set_current=False)
        trace.finish_success({
            "status": audit["result_status"],
            "accepted_count": len(generated), "discarded_count": len(rejected_ids),
            "added_relations": [item.model_dump(mode="json") for item in generated],
        })
        return RelationRunResponse(
            status=audit["result_status"], added_relations=generated,
            discarded_count=len(rejected_ids))
    except Exception as exc:
        if isinstance(exc, AppError):
            failure = exc
        elif isinstance(exc, OSError):
            failure = AppError("relation_storage_failed", "Relation artifacts could not be persisted", 500)
        else:
            failure = AppError("relation_internal_error", "Unexpected Relation failure; see run audit", 500)
        run.status = RunStatus.failed
        run.error_message = f"{failure.code}: {failure}"
        audit["status"] = "failed"
        audit["result_status"] = "failed"
        audit["error_code"] = failure.code
        audit["errors"] = failure.details.get("errors", [{
            "item_id": BATCH_ITEM_ID, "field_path": "$",
            "error_code": failure.code, "message": str(failure)}])
        if isinstance(exc, llm_client.LLMFailure):
            if audit["active_step"] == "relation_validation_repair":
                audit.setdefault("repair", {})["raw_response"] = exc.response
            else:
                audit["raw_response"] = exc.response
        trace.finish_error(exc, step=audit["active_step"])
        try:
            if not artifact_path(case.case_id, run.input_snapshot_path).exists():
                write_json(artifact_path(case.case_id, run.input_snapshot_path), {
                    "run_id": run_id, "run_name": trace.run_name, "workflow_id": workflow_id,
                    "case_id": case.case_id, "input_preparation_failed": True,
                })
            _write_audit(case.case_id, run, audit)
            latest = load_case_snapshot(case.case_id)
            if latest is not None:
                latest.stage_runs.append(run)
                latest.updated_at = now_iso()
                save_case(latest, set_current=False)
        except (OSError, AppError, ValidationError) as audit_exc:
            raise AppError("relation_audit_persistence_failed", "Relation failed and its audit could not be saved", 500,
                           details={"run_id": run_id, "cause_code": failure.code}) from audit_exc
        raise AppError(failure.code, str(failure), failure.status_code,
                       details={**failure.details, "run_id": run_id}) from exc
