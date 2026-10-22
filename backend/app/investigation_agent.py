"""Finite, case-bound investigation through native read-only function calls."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
import re
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app import investigation_tools as tools, llm_client
from app.case_service import now_iso
from app.case_store import artifact_path, write_json
from app.errors import AppError
from app.observability import RunTrace
from app.prompt_loader import load_prompt
from app.schemas import CaseData, InfoUnitType, RelationType

MAX_TOOL_CALLS = 8
PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts/investigation_agent_prompt.md"
BoundedText = Annotated[str, Field(min_length=1, max_length=600, pattern=r"\S")]
IDs = Annotated[list[BoundedText], Field(max_length=80)]


class InvestigationOutput(BaseModel):
    """Transport-only contracts; the persistent CaseData Schema is unchanged."""
    model_config = ConfigDict(extra="forbid", strict=True)


class EvidencePointer(InvestigationOutput):
    info_unit_id: BoundedText
    source_ref_index: Annotated[int, Field(ge=0)]


Pointers = Annotated[list[EvidencePointer], Field(max_length=64)]


class Finding(InvestigationOutput):
    statement: BoundedText
    info_unit_ids: IDs
    relation_ids: IDs
    evidence_refs: Pointers


class InvestigationResult(InvestigationOutput):
    goal: Annotated[str, Field(min_length=1, max_length=2000, pattern=r"\S")]
    findings: Annotated[list[Finding], Field(max_length=12)]
    relevant_info_unit_ids: IDs
    relevant_relation_ids: IDs
    evidence_refs: Pointers
    open_questions: Annotated[list[BoundedText], Field(max_length=20)]
    completion_status: Literal["completed", "evidence_insufficient", "budget_exhausted"]


def _function(name: str, description: str, properties: dict) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description, "strict": True,
        "parameters": {"type": "object", "properties": properties,
                       "required": list(properties), "additionalProperties": False},
    }}


def tool_definitions() -> list[dict]:
    ids = {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": tools.MAX_NODE_IDS}
    limit = {"type": ["integer", "null"], "minimum": 1, "maximum": tools.MAX_LIMIT}
    return [
        _function("search_info_units", "按关键词检索节点摘要；empty 只表示此次未命中，truncated 表示仍有未返回结果。", {
            "query": {"type": "string", "minLength": 1, "maxLength": tools.MAX_QUERY_CHARS},
            "types": {"type": ["array", "null"], "items": {"type": "string", "enum": [t.value for t in InfoUnitType]}},
            "limit": limit,
        }),
        _function("get_relations", "查询指定节点持久化的一跳入边和出边，保留原始方向；不推导多跳关系。", {
            "info_unit_ids": ids,
            "relation_types": {"type": ["array", "null"], "items": {"type": "string", "enum": [t.value for t in RelationType]}},
            "limit": limit,
        }),
        _function("get_evidence", "读取节点精确来源引用与偏移；最多 8 个完整引用、4000 字符，不读取无关内容。", {
            "info_unit_ids": ids,
        }),
    ]


def _strict_json(raw: str) -> object:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    def non_json(_value):
        raise ValueError("Non-JSON constant")

    return json.loads(raw, object_pairs_hook=unique, parse_constant=non_json)


def _arguments(name: str, raw: str) -> dict:
    if len(raw) > 4000:
        raise AppError("invalid_tool_arguments", "Tool arguments exceed 4000 characters")
    try:
        args = _strict_json(raw)
    except (ValueError, TypeError, RecursionError) as exc:
        raise AppError("invalid_tool_arguments", "Tool arguments must be strict JSON") from exc
    fields = {
        "search_info_units": ({"query"}, {"query", "types", "limit"}),
        "get_relations": ({"info_unit_ids"}, {"info_unit_ids", "relation_types", "limit"}),
        "get_evidence": ({"info_unit_ids"}, {"info_unit_ids"}),
    }
    if name not in fields:
        raise AppError("unknown_tool", "Only the three read-only investigation tools are allowed")
    required, allowed = fields[name]
    if not isinstance(args, dict) or not required <= args.keys() or not args.keys() <= allowed:
        raise AppError("invalid_tool_arguments", "Missing or unsupported tool argument fields")
    if name != "get_evidence":
        args["limit"] = tools.DEFAULT_LIMIT if args.get("limit") is None else args["limit"]
        args.setdefault("types" if name == "search_info_units" else "relation_types", None)
    return args


def _fingerprint(name: str, args: dict) -> str:
    normalized = deepcopy(args)
    if name == "search_info_units" and isinstance(args.get("query"), str):
        normalized["query"] = sorted(set(re.findall(r"\w+", tools._normalized(args["query"]))))
    if name == "search_info_units" and normalized.get("types") is None:
        normalized["types"] = [kind.value for kind in InfoUnitType]
    if name == "get_relations" and normalized.get("relation_types") is None:
        normalized["relation_types"] = [kind.value for kind in RelationType]
    # Evidence order affects which complete references fit the bounded prefix.
    unordered_fields = ["types", "relation_types"]
    if name == "get_relations":
        unordered_fields.append("info_unit_ids")
    for field in unordered_fields:
        if isinstance(normalized.get(field), list):
            normalized[field] = sorted(normalized[field], key=lambda value: json.dumps(value, sort_keys=True))
    return name + ":" + json.dumps(normalized, ensure_ascii=False, sort_keys=True)


def _remember(name: str, result: dict, seen: dict) -> None:
    for item in result["items"]:
        if name == "search_info_units":
            seen["units"].add(item["id"])
        elif name == "get_relations":
            relation = item["relation"]
            seen["relations"].add(relation["id"])
            seen["units"].update((relation["source_id"], relation["target_id"]))
        elif name == "get_evidence":
            seen["units"].add(item["info_unit_id"])
            seen["evidence"].add((item["info_unit_id"], item["source_ref_index"]))


def _validate_result(raw: str, goal: str, case: CaseData, seen: dict) -> InvestigationResult:
    try:
        parsed = _strict_json(raw)
        result = InvestigationResult.model_validate(parsed)
    except (ValueError, TypeError, RecursionError, ValidationError) as exc:
        raise AppError("investigation_result_invalid", "Final investigation result violates its JSON contract") from exc
    units = {unit.id: unit for unit in case.info_units}
    relations = {relation.id for relation in case.relations}

    def check(ids, available):
        if len(ids) != len(set(ids)) or not set(ids) <= available:
            raise AppError("invalid_investigation_reference", "A final reference is duplicate, nonexistent or was not retrieved")

    check(result.relevant_info_unit_ids, seen["units"] & units.keys())
    check(result.relevant_relation_ids, seen["relations"] & relations)
    pointers = [(ref.info_unit_id, ref.source_ref_index) for ref in result.evidence_refs]
    check(pointers, seen["evidence"])
    for uid, index in pointers:
        if uid not in units or index >= len(units[uid].source_refs) or uid not in result.relevant_info_unit_ids:
            raise AppError("invalid_investigation_reference", "Invalid SourceRef index")
    if result.goal != goal:
        raise AppError("investigation_result_invalid", "Final goal must match the requested goal")
    for finding in result.findings:
        check(finding.info_unit_ids, set(result.relevant_info_unit_ids))
        check(finding.relation_ids, set(result.relevant_relation_ids))
        refs = [(ref.info_unit_id, ref.source_ref_index) for ref in finding.evidence_refs]
        check(refs, set(pointers))
        if not (finding.info_unit_ids or finding.relation_ids or refs):
            raise AppError("invalid_investigation_reference", "Each finding requires a retrieved basis")
    return result


def _write_audit(case_id: str, path: str, payload: dict) -> None:
    try:
        write_json(artifact_path(case_id, path), llm_client.redact(payload))
    except (OSError, AppError) as exc:
        raise AppError("investigation_audit_failed", "Cannot persist the investigation audit", 500) from exc


async def run_investigation(case: CaseData, goal: str,
                            selected_info_unit_ids: list[str] | None = None) -> dict:
    """Investigate without changing CaseData; only separate runs/* audits are written.

    Up to 8 tool attempts and one finalization turn. A duplicate is not executed.
    Invalid final citations fail explicitly; there is no model repair or retry.
    """
    if not isinstance(goal, str) or not goal.strip() or len(goal) > 2000:
        raise AppError("invalid_investigation_goal", "goal must contain 1 to 2000 nonblank characters")
    bound_case = case.model_copy(deep=True)
    selected = [] if selected_info_unit_ids is None or selected_info_unit_ids == [] else tools._selected_units(
        bound_case, selected_info_unit_ids)
    counts = Counter(unit.type.value for unit in bound_case.info_units)
    initial_input = {
        "goal": goal, "core_issue": bound_case.core_issue[:500] if bound_case.core_issue else None,
        "info_unit_type_counts": {kind.value: counts[kind.value] for kind in InfoUnitType},
        "selected_info_units": [tools._summary(unit) for unit in selected],
    }
    run_id = f"investigation-{uuid4().hex}"
    created_at = now_iso()
    trace = RunTrace(case_id=bound_case.case_id, case_title=bound_case.case_title, stage="Investigation Agent",
                     run_id=run_id, workflow_id=run_id, created_at=created_at, initial_input=initial_input)
    audit = {"run_id": run_id, "run_name": trace.run_name, "stage": "investigation_agent",
             "workflow_id": run_id, "created_at": created_at, "status": "started",
             "model_calls": [], "tool_calls": [], "stop_reason": None, "result": None}
    output_path = f"runs/{run_id}.output.json"
    seen = {"units": {unit.id for unit in selected}, "relations": set(), "evidence": set()}
    fingerprints: set[str] = set()
    call_ids: set[str] = set()
    limitations: list[str] = []
    stop_reason = None
    attempts = 0
    executed = 0
    try:
        config = llm_client.load_config()
        prompt = load_prompt(PROMPT_PATH, "investigation_agent")
        definitions = tool_definitions()
        schema = InvestigationResult.model_json_schema()
        metadata = prompt.metadata(config.model)
        messages = [{"role": "system", "content": prompt.content},
                    {"role": "user", "content": json.dumps(initial_input, ensure_ascii=False)}]
        _write_audit(bound_case.case_id, f"runs/{run_id}.input.json", {
            "run_id": run_id, "model_input": initial_input, "system_prompt": prompt.content,
            "tools": definitions, "schema": schema, "configuration": config.public_metadata(), **metadata})
        while True:
            finalizing = stop_reason is not None
            choice = "none" if finalizing else "auto"
            payload = llm_client.tool_request_payload(config, messages, definitions, schema, tool_choice=choice)
            call_audit = {"step": len(audit["model_calls"]) + 1, **metadata, "request": deepcopy(payload)}
            audit["model_calls"].append(call_audit)
            _write_audit(bound_case.case_id, output_path, audit)
            with trace.observation(
                "Investigation Agent · model call", as_type="generation", input=payload,
                model=config.model, version=prompt.version,
                model_parameters={"max_completion_tokens": config.max_completion_tokens}, metadata=metadata,
            ) as generation:
                reply = await llm_client.call_tools(config, messages, definitions, schema, tool_choice=choice)
                generation.record_reply(reply)
            call_audit.update(raw_response=reply.response, message=reply.message)
            calls = reply.message.get("tool_calls")
            if not calls:
                with trace.observation("Investigation Agent · validate result", as_type="guardrail",
                                       input=reply.content) as validation:
                    result = _validate_result(reply.content, goal, bound_case, seen)
                    if result.completion_status == "budget_exhausted" and attempts != MAX_TOOL_CALLS:
                        raise AppError("investigation_result_invalid", "Tool budget has not been exhausted")
                    if attempts == MAX_TOOL_CALLS:
                        result.completion_status = "budget_exhausted"
                    elif stop_reason or limitations or not result.findings or not result.evidence_refs:
                        if result.completion_status == "completed":
                            result.completion_status = "evidence_insufficient"
                    if not result.evidence_refs:
                        limitations.append("最终结果未使用可精确回溯的原文证据；摘要和空结果均不能证明事实成立或证据不存在。")
                    result.open_questions = list(dict.fromkeys([*result.open_questions, *limitations]))
                    try:
                        result = InvestigationResult.model_validate(result.model_dump())
                    except ValidationError as exc:
                        raise AppError("investigation_result_invalid", "Final result including runtime limits exceeds its contract") from exc
                    validation.update(output={"valid": True, "result": result.model_dump(mode="json")})
                stop_reason = stop_reason or "model_finished"
                output = {**result.model_dump(mode="json"), "run_id": run_id, "stop_reason": stop_reason,
                          "tool_call_count": executed, "tool_attempt_count": attempts,
                          "model_call_count": len(audit["model_calls"])}
                audit.update(status="success", stop_reason=stop_reason, result=output)
                _write_audit(bound_case.case_id, output_path, audit)
                with trace.observation("Investigation Agent · stop", metadata={
                    "stop_reason": stop_reason, "completion_status": result.completion_status,
                    "tool_call_count": executed, "tool_attempt_count": attempts,
                    "model_call_count": len(audit["model_calls"]),
                }) as stop_span:
                    stop_span.update(output={"status": result.completion_status})
                trace.finish_success(output)
                return output
            if finalizing or not isinstance(calls, list) or len(calls) != 1:
                raise AppError("investigation_protocol_invalid", "Only one tool call is allowed before finalization")
            call = calls[0]
            if call["id"] in call_ids:
                raise AppError("investigation_protocol_invalid", "Tool call IDs must be unique within a run")
            call_ids.add(call["id"])
            messages.append(deepcopy(reply.message))
            name, raw = call["function"]["name"], call["function"]["arguments"]
            attempts += 1
            tool_audit = {"call_id": call["id"], "name": name, "raw_arguments": raw, "executed": False}
            audit["tool_calls"].append(tool_audit)
            _write_audit(bound_case.case_id, output_path, audit)
            with trace.observation("Investigation Agent · tool call", as_type="tool",
                                   input={"name": name, "arguments": raw}, metadata={
                                       "tool_name": name if name in {"search_info_units", "get_relations", "get_evidence"}
                                       else "unsupported_tool", "attempt": attempts,
                                   }) as tool_span:
                try:
                    args = _arguments(name, raw)
                    fingerprint = _fingerprint(name, args)
                    tool_audit["arguments"] = args
                except AppError as exc:
                    fingerprint = name + ":invalid:" + raw
                    response = tools._failure(exc)
                else:
                    response = None
                if fingerprint in fingerprints:
                    stop_reason = "duplicate_query"
                    limitations.append("等价重复查询已被阻止，调查停止继续调用工具。")
                    response = tools._failure(AppError("duplicate_query", "Equivalent query was already attempted"))
                else:
                    fingerprints.add(fingerprint)
                    if response is None:
                        tool_audit["executed"] = True
                        executed += 1
                        try:
                            response = {"search_info_units": tools.search_info_units,
                                        "get_relations": tools.get_relations,
                                        "get_evidence": tools.get_evidence}[name](bound_case, **args)
                        except Exception as tool_error:
                            tool_audit["exception_type"] = type(tool_error).__name__
                            tool_audit["error_code"] = getattr(tool_error, "code", "investigation_tool_failed")
                            response = tools._failure(AppError("investigation_tool_failed", "Read-only tool failed"), status="error")
                _remember(name, response, seen)
                if response["status"] in {"truncated", "invalid_request", "error"}:
                    limitations.append(f"{name} 返回 {response['status']}，该查询未获得完整有效结果。")
                if name == "get_relations" and any(row["relation"]["evidence_ids_truncated"] for row in response["items"]):
                    limitations.append("部分关系的依据 ID 列表被截断。")
                tool_audit["result"] = deepcopy(response)
                tool_span.update(output=response)
            messages.append({"role": "tool", "tool_call_id": call["id"],
                             "content": json.dumps(response, ensure_ascii=False)})
            if attempts == MAX_TOOL_CALLS:
                stop_reason = "tool_budget_exhausted"
                limitations.append("已达到 8 次工具调用尝试上限，未继续检索。")
            if stop_reason:
                audit["stop_reason"] = stop_reason
                messages.append({"role": "system", "content":
                                 f"工具阶段已停止：{stop_reason}。仅总结已见数据，保留调查限制，不得再请求工具。"})
            _write_audit(bound_case.case_id, output_path, audit)
    except Exception as exc:
        audit.update(status="failed", stop_reason=stop_reason or "error", error=trace.error_payload(exc))
        if isinstance(exc, llm_client.LLMFailure):
            audit["provider_response"] = exc.response
        trace.finish_error(exc)
        _write_audit(bound_case.case_id, output_path, audit)
        if isinstance(exc, AppError):
            exc.details = {**exc.details, "run_id": run_id}
        raise
