"""Incremental Relation generation and single-relation persistence."""
from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from app import llm_client
from app.api_models import CreateRelationInput, RelationRunResponse, UpdateRelationInput
from app.case_service import now_iso
from app.case_store import artifact_path, case_dir, load_case_snapshot, save_case, write_json
from app.errors import AppError
from app.schemas import (
    CaseData, PipelineStage, Relation, RelationInput, RelationOutput, RelationType, RunStatus, StageRun,
)
from app.validation import validate_relations

PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts/relation_system_prompt.md"


def _relation_key(relation: Relation) -> tuple[str, str, RelationType]:
    source, target = relation.source_id, relation.target_id
    if relation.relation_type == RelationType.contradict and target < source:
        source, target = target, source
    return source, target, relation.relation_type


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


def _prompt_with_version() -> tuple[str, str]:
    try:
        prompt = PROMPT_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise AppError("relation_prompt_unavailable", "Relation prompt could not be read", 500) from exc
    if not prompt.strip():
        raise AppError("relation_prompt_unavailable", "Relation prompt is empty", 500)
    digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
    return prompt, f"v0.3-relation-1:{digest}"


def _snapshot_bytes(case_id: str) -> bytes:
    return (case_dir(case_id) / "case.json").read_bytes()


def _write_audit(case_id: str, run: StageRun, audit: dict) -> None:
    write_json(artifact_path(case_id, run.output_snapshot_path), llm_client.redact(audit))
    write_json(artifact_path(case_id, f"runs/{run.id}.stage.json"), run.model_dump(mode="json"))


async def run_relation(case: CaseData, selected_info_unit_ids: list[str] | None = None) -> RelationRunResponse:
    run_id = f"run-{uuid4().hex}"
    run = StageRun(
        id=run_id, stage=PipelineStage.relation, status=RunStatus.failed, model=None,
        prompt_version=None, input_snapshot_path=f"runs/{run_id}.input.json",
        output_snapshot_path=f"runs/{run_id}.output.json", error_message=None, created_at=now_iso(),
    )
    scope_ids = selected_info_unit_ids or [unit.id for unit in case.info_units]
    audit: dict = {"run_id": run_id, "status": "started", "scope_info_unit_ids": scope_ids,
                   "raw_response": None, "model_content": None, "added_relations": [], "errors": []}
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
                "run_id": run_id, "case_id": case.case_id, "model_skipped": "fewer_than_two_info_units",
            })
            run.status = RunStatus.success
            audit["status"] = "success"
            audit["model_skipped"] = "fewer_than_two_info_units"
            _write_audit(case.case_id, run, audit)
            candidate = case.model_copy(deep=True)
            candidate.stage_runs.append(run)
            candidate.updated_at = now_iso()
            save_case(candidate, set_current=False)
            return RelationRunResponse(added_relations=[])

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
        prompt, version = _prompt_with_version()
        run.prompt_version = version
        config = llm_client.load_config()
        run.model = config.model
        write_json(artifact_path(case.case_id, run.input_snapshot_path), llm_client.redact({
            "run_id": run_id, "model_input": model_input, "system_prompt": prompt,
            "prompt_version": version, "configuration": config.public_metadata(),
            "case_snapshot_sha256": hashlib.sha256(initial_snapshot).hexdigest(),
        }))
        _write_audit(case.case_id, run, audit)
        reply = await llm_client.call_structured(config, prompt, model_input, RelationOutput.model_json_schema())
        audit["raw_response"] = reply.response
        audit["model_content"] = reply.content
        try:
            output = RelationOutput.model_validate_json(reply.content)
        except (ValidationError, ValueError) as exc:
            raise AppError("relation_schema_invalid", "Model output does not match RelationOutput", 422) from exc
        validation = validate_relations(output, context)
        if not validation.valid:
            raise AppError("relation_validation_failed", "Generated Relations are invalid", 422,
                           details={"errors": [error.model_dump(mode="json") for error in validation.errors]})

        existing_keys = {_relation_key(item) for item in case.relations}
        seen_keys: set[tuple[str, str, RelationType]] = set()
        generated: list[Relation] = []
        ids = {item.id for item in case.relations}
        for proposed in output.relations:
            key = _relation_key(proposed)
            if key in existing_keys or key in seen_keys:
                continue
            seen_keys.add(key)
            generated.append(proposed.model_copy(update={"id": _new_relation_id(ids)}))

        if _snapshot_bytes(case.case_id) != initial_snapshot:
            raise AppError("case_changed_during_relation", "Case changed while the model was running; Relations were not committed", 409)
        candidate = _validated_candidate(case, [*case.relations, *generated])
        run.status = RunStatus.success
        audit["status"] = "success"
        audit["added_relations"] = [item.model_dump(mode="json") for item in generated]
        _write_audit(case.case_id, run, audit)
        candidate.stage_runs.append(run)
        if _snapshot_bytes(case.case_id) != initial_snapshot:
            raise AppError("case_changed_during_relation", "Case changed while the model was running; Relations were not committed", 409)
        save_case(candidate, set_current=False)
        return RelationRunResponse(added_relations=generated)
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
        audit["error_code"] = failure.code
        audit["errors"] = failure.details.get("errors", [{"code": failure.code, "message": str(failure)}])
        if isinstance(exc, llm_client.LLMFailure):
            audit["raw_response"] = exc.response
        try:
            if not artifact_path(case.case_id, run.input_snapshot_path).exists():
                write_json(artifact_path(case.case_id, run.input_snapshot_path), {
                    "run_id": run_id, "case_id": case.case_id, "input_preparation_failed": True,
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
