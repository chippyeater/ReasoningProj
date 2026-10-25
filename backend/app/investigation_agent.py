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
from app.reasoning_models import ReasoningProposal
from app.investigation_state import EvidencePointer, KnowledgeState, Question, QuestionState, model_context, _preview

MAX_TOOL_CALLS = 8  # Read attempts, reserved independently from artifact submissions.
MAX_PROPOSAL_CALLS = 6
MAX_QUESTION_CALLS = 8
MAX_FINISH_CALLS = 3
MAX_TOTAL_CALLS = MAX_TOOL_CALLS + MAX_PROPOSAL_CALLS + MAX_QUESTION_CALLS + MAX_FINISH_CALLS
MAX_MODEL_CALLS = MAX_TOTAL_CALLS + 3
MAX_SUMMARY_CHARACTERS = 240  # Soft target is 40–80; allow a little more for complex tasks.
MAX_NO_PROGRESS = 3
READ_TOOLS = {"search_info_units": tools.search_info_units, "get_relations": tools.get_relations, "get_evidence": tools.get_evidence}
PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts/investigation_agent_prompt.md"
BoundedText = Annotated[str, Field(min_length=1, max_length=600, pattern=r"\S")]
IDs = Annotated[list[BoundedText], Field(max_length=80)]


class InvestigationOutput(BaseModel):
    """Transport-only contracts; the persistent CaseData Schema is unchanged."""
    model_config = ConfigDict(extra="forbid", strict=True)


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
    completion_status: Literal["completed", "evidence_insufficient", "budget_exhausted", "unsupported"]
    proposals: Annotated[list[ReasoningProposal], Field(max_length=6)] = Field(default_factory=list)


def _function(name: str, description: str, properties: dict) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description, "strict": True,
        "parameters": {"type": "object", "properties": properties,
                       "required": list(properties), "additionalProperties": False},
    }}


def tool_definitions() -> list[dict]:
    ids = {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": tools.MAX_NODE_IDS}
    limit = {"type": ["integer", "null"], "minimum": 1, "maximum": tools.MAX_LIMIT}
    definitions = [
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
    from app.investigation_actions import PROPOSAL_TOOLS, finish_investigation
    schema = ReasoningProposal.model_json_schema()
    props = schema["properties"]
    common = {key: props[key] for key in ("rationale", "info_unit_ids", "evidence_refs")}
    grounded = {**common, "info_unit_ids": {**props["info_unit_ids"], "minItems": 1},
                "evidence_refs": {**props["evidence_refs"], "minItems": 1}}
    content = next(part for part in props["content"]["anyOf"] if part.get("type") != "null")
    text_id = {"type": "string", "minLength": 1, "pattern": r"\S"}
    specifications = {
        "propose_hypothesis": {**common, "content": content},
        "propose_evidence_link": {"hypothesis_id": {"anyOf": [text_id, {"type": "null"}]}, "info_unit_id": text_id,
            "hypothesis_proposal_ids": {**props["hypothesis_proposal_ids"], "maxItems": 1},
            "meaning": next(p for p in props["meaning"]["anyOf"] if p.get("type") != "null"),
            "rationale": props["rationale"], "evidence_refs": grounded["evidence_refs"]},
        "propose_evidence_gap": {**common, "content": content, "hypothesis_ids": props["hypothesis_ids"],
            "hypothesis_proposal_ids": props["hypothesis_proposal_ids"]},
        "propose_hypothesis_status": {**grounded, "hypothesis_id": text_id,
            "judgment": next(p for p in props["judgment"]["anyOf"] if p.get("type") != "null")},
    }
    for name, properties in specifications.items():
        definition = _function(name, PROPOSAL_TOOLS[name].__doc__, deepcopy(properties))
        definition["function"]["parameters"]["$defs"] = schema["$defs"]
        definitions.append(definition)
    question_schema = Question.model_json_schema()
    question_defs = question_schema.pop("$defs")
    update_schema = {"type": "array", "items": question_schema, "maxItems": 20}
    definition = _function("update_questions", "Batch create/adjust run-local Questions. Q1 retains the full original goal. Track independently requested answers/conditions as distinct Questions. Only mark answered when resolved; otherwise block with specific missing condition/kind. Preserve plan IDs/kinds and validated Proposal bindings; descriptions may be rephrased. Link only actual same-run proposal_id of matching kind, submitted under this Question's question_ids with related materials/dependencies. No canvas writes.", {"questions": update_schema})
    definition["function"]["parameters"]["$defs"] = question_defs
    definitions.append(definition)
    finding_schema = Finding.model_json_schema()
    finding_defs = finding_schema.pop("$defs")
    definition = _function("finish_investigation", finish_investigation.__doc__, {
        "answer": {"type": "string", "minLength": 1, "maxLength": 1000},
        "findings": {"type": "array", "items": finding_schema, "maxItems": 12},
        "question_updates": update_schema,
        "completion_status": InvestigationResult.model_json_schema()["properties"]["completion_status"],
    })
    definition["function"]["parameters"]["$defs"] = {**question_defs, **finding_defs}
    definitions.append(definition)
    for definition in definitions:
        if definition["function"]["name"] in READ_TOOLS or definition["function"]["name"] in PROPOSAL_TOOLS:
            properties = definition["function"]["parameters"]["properties"]
            properties["question_ids"] = {"type": ["array", "null"], "items": {"type": "string"}, "maxItems": 20,
                "description": "Existing Question IDs; null associates the current Questions automatically"}
            definition["function"]["parameters"]["required"].append("question_ids")
    return definitions


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
    if len(raw) > (4000 if name in READ_TOOLS else 64000 if name in {"update_questions", "finish_investigation"} else 12000):
        raise AppError("invalid_tool_arguments", "Tool arguments exceed their bounded size")
    try:
        args = _strict_json(raw)
    except (ValueError, TypeError, RecursionError) as exc:
        raise AppError("invalid_tool_arguments", "Tool arguments must be strict JSON", details={
            "category": "json", "errors": [{"loc": [], "code": "invalid_json",
                **{key: getattr(exc, key) for key in ("lineno", "colno", "pos") if hasattr(exc, key)}}]}) from exc
    fields = {d["function"]["name"]: (set(d["function"]["parameters"]["required"]),
              set(d["function"]["parameters"]["properties"])) for d in tool_definitions()}
    fields["search_info_units"] = ({"query"}, fields["search_info_units"][1])
    fields["get_relations"] = ({"info_unit_ids"}, fields["get_relations"][1])
    for tool_name in READ_TOOLS.keys() | {"propose_hypothesis", "propose_evidence_link", "propose_evidence_gap", "propose_hypothesis_status"}:
        fields[tool_name] = (fields[tool_name][0] - {"question_ids"}, fields[tool_name][1])
    # Existing formal-Hypothesis calls remain valid; native schemas ask for an explicit empty list.
    fields["propose_evidence_link"] = (fields["propose_evidence_link"][0] - {"hypothesis_proposal_ids"}, fields["propose_evidence_link"][1])
    if name not in fields:
        raise AppError("unknown_tool", "Only registered investigation and pending-artifact tools are allowed")
    required, allowed = fields[name]
    if not isinstance(args, dict) or not required <= args.keys() or not args.keys() <= allowed:
        missing = sorted(required - args.keys()) if isinstance(args, dict) else []
        errors = [{"loc": [field], "code": "missing", "message": "Required tool argument is missing",
                   "fix": "Supply the field; question_updates=[] reuses validated Questions"} for field in missing]
        if not isinstance(args, dict) or args.keys() - allowed:
            errors.append({"loc": ["<extra>"] if isinstance(args, dict) else [], "code": "invalid_arguments",
                           "message": "Unsupported fields or non-object arguments", "fix": "Submit only fields in the tool schema"})
        raise AppError("invalid_tool_arguments", "Missing or unsupported tool argument fields", details={
            "category": "structure", "errors": errors})
    if name in {"search_info_units", "get_relations"}:
        args["limit"] = tools.DEFAULT_LIMIT if args.get("limit") is None else args["limit"]
        args.setdefault("types" if name == "search_info_units" else "relation_types", None)
    if name == "propose_evidence_link":
        args.setdefault("hypothesis_proposal_ids", [])
    return args


def _fingerprint(name: str, args: dict) -> str:
    normalized = deepcopy(args)
    normalized.pop("question_ids", None)
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


def _validation_details(exc: ValidationError) -> dict:
    errors = []
    for error in exc.errors(include_input=False, include_context=False, include_url=False):
        loc = list(error["loc"])
        if error["type"] == "extra_forbidden":
            loc[-1] = "<extra>"
        code = error["type"]
        if "Duplicate" in error["msg"]:
            code = "duplicate_reference"
            loc += [next((field for field in ("hypothesis_proposal_ids", "hypothesis_proposal_indices", "info_unit_ids", "hypothesis_ids")
                          if field in error["msg"]), "references")]
        if "proposals require info_unit_ids and evidence_refs" in error["msg"]:
            code, loc = "evidence_required", [*loc, "evidence_refs"]
        errors.append({"loc": loc, "code": code, "message": error["msg"],
                       "category": "proposal_semantics" if error["type"] == "value_error" else "structure"})
    categories = {e["category"] for e in errors}
    return {"category": next(iter(categories)) if len(categories) == 1 else "mixed", "errors": errors}


def _validate_result(raw: str, goal: str, case: CaseData, seen: dict, work_context: dict | None = None) -> InvestigationResult:
    try:
        parsed = _strict_json(raw)
    except (ValueError, TypeError, RecursionError) as exc:
        location = {key: getattr(exc, key) for key in ("lineno", "colno", "pos") if hasattr(exc, key)}
        raise AppError("investigation_result_invalid", "Final result is not strict JSON", details={
            "category": "json", "errors": [{"loc": [], "code": "invalid_json", **location}]}) from exc
    try:
        result = InvestigationResult.model_validate(parsed)
    except ValidationError as exc:
        raise AppError("investigation_result_invalid", "Artifact contract validation failed", details=_validation_details(exc)) from exc
    units = {unit.id: unit for unit in case.info_units}
    relations = {relation.id for relation in case.relations}
    issues = []

    def invalid(code, category, loc, message, detail_code=None):
        issues.append({"loc": loc, "code": detail_code or code, "message": message,
                       "category": category, "error_code": code,
                       "fix": "Use obtained current-case IDs/SourceRefs and valid same-run Proposal dependencies"})

    def check(ids, available, loc, known=None):
        visited = set()
        for index, value in enumerate(ids):
            detail = ("duplicate_reference" if value in visited else "reference_not_found" if known is not None and value not in known
                      else "reference_not_obtained" if value not in available else None)
            if detail:
                invalid("invalid_investigation_reference", "reference", [*loc, index],
                              "Use a unique, current-case reference supplied in context or tool results", detail)
            visited.add(value)

    check(result.relevant_info_unit_ids, seen["units"] & units.keys(), ["relevant_info_unit_ids"], units)
    check(result.relevant_relation_ids, seen["relations"] & relations, ["relevant_relation_ids"], relations)
    pointers = [(ref.info_unit_id, ref.source_ref_index) for ref in result.evidence_refs]
    for offset, (uid, index) in enumerate(pointers):
        if uid not in units or index >= len(units[uid].source_refs) or uid not in result.relevant_info_unit_ids:
            invalid("invalid_investigation_reference", "reference", ["evidence_refs", offset], "Invalid SourceRef index or InfoUnit",
                          "reference_not_found" if uid not in units else "reference_out_of_range" if index >= len(units[uid].source_refs) else "reference_not_obtained")
    check(pointers, seen["evidence"], ["evidence_refs"])
    if result.goal != goal:
        invalid("investigation_result_invalid", "result_semantics", ["goal"], "Final goal must match the requested goal")
    if result.completion_status == "unsupported" and (
            result.findings or result.relevant_info_unit_ids or result.relevant_relation_ids
            or result.evidence_refs or result.proposals or not result.open_questions):
        invalid("investigation_result_invalid", "result_semantics", ["completion_status"], "Unsupported requests require an explanation and no findings")
    for offset, finding in enumerate(result.findings):
        check(finding.info_unit_ids, set(result.relevant_info_unit_ids), ["findings", offset, "info_unit_ids"])
        check(finding.relation_ids, set(result.relevant_relation_ids), ["findings", offset, "relation_ids"])
        refs = [(ref.info_unit_id, ref.source_ref_index) for ref in finding.evidence_refs]
        check(refs, set(pointers), ["findings", offset, "evidence_refs"])
        if not (finding.info_unit_ids or finding.relation_ids or refs):
            invalid("invalid_investigation_reference", "reference", ["findings", offset], "Each finding requires a retrieved basis")
    targets = {obj["id"] for kind in ("hypotheses", "gaps")
               for obj in (work_context or {}).get("reasoning", {}).get(kind, [])}
    hypothesis_ids = {obj["id"] for obj in (work_context or {}).get("reasoning", {}).get("hypotheses", [])}
    from app.reasoning_proposals import validate_proposal_dependencies
    try:
        validate_proposal_dependencies(result.proposals)
    except AppError as exc:
        from app.investigation_state import validation_issues
        issues.extend(validation_issues(exc))
    for offset, proposal in enumerate(result.proposals):
        if not work_context:
            invalid("invalid_reasoning_proposal", "proposal_semantics", ["proposals", offset], "Proposals require a bound Issue")
        check(proposal.info_unit_ids, set(result.relevant_info_unit_ids), ["proposals", offset, "info_unit_ids"])
        check(proposal.hypothesis_ids, hypothesis_ids & targets, ["proposals", offset, "hypothesis_ids"])
        check([(p.info_unit_id, p.source_ref_index) for p in proposal.evidence_refs], set(pointers), ["proposals", offset, "evidence_refs"])
        if any(p.info_unit_id not in proposal.info_unit_ids for p in proposal.evidence_refs):
            invalid("invalid_reasoning_proposal", "reference", ["proposals", offset, "evidence_refs"], "Proposal evidence must refer to its retrieved InfoUnits")
    if issues:
        first = issues[0]
        raise AppError(first.get("error_code", "invalid_reasoning_proposal"), "Artifact validation failed; fix all listed fields",
                       details={"category": first.get("category", "reference"), "errors": issues})
    return result


def _write_audit(case_id: str, path: str, payload: dict) -> None:
    try:
        write_json(artifact_path(case_id, path), llm_client.redact(payload))
    except (OSError, AppError) as exc:
        raise AppError("investigation_audit_failed", "Cannot persist the investigation audit", 500) from exc


def _tool_recovery(response, name):
    """Add actionable diagnostics to this loop's existing tool error envelope."""
    if response["status"] not in {"error", "invalid_request"}:
        return
    error = response["error"]
    details = error.setdefault("validation_details", {})
    if not details:
        details.update(category="tool", errors=[{"loc": [], "code": error["code"]}])
        if error["code"] == "unknown_info_unit":
            details.update(category="reference", errors=[{"loc": ["info_unit_ids"], "code": "reference_not_found"}])
    for item in details["errors"]:
        loc = item.get("loc", [])
        argument_loc = loc[2:] if loc[:1] == ["proposals"] else list(loc)
        if argument_loc[:1] == ["relevant_info_unit_ids"]:
            argument_loc[0] = "info_unit_ids"
        if name == "propose_evidence_link" and argument_loc[:1] in (["info_unit_ids"], ["hypothesis_ids"]):
            argument_loc = ["info_unit_id" if argument_loc[0] == "info_unit_ids" else "hypothesis_id"]
        if name == "finish_investigation" and argument_loc[:1] in (["info_unit_ids"], ["relevant_relation_ids"], ["evidence_refs"]):
            argument_loc = ["findings"]
        item.setdefault("argument_loc", argument_loc)
        if item["code"] == "reference_not_obtained" and argument_loc[:1] in (["hypothesis_ids"], ["hypothesis_id"]):
            item["code"] = "invalid_hypothesis_target"
    codes = {item["code"] for item in details["errors"]}
    if codes & {"goal_scope_changed", "goal_scope_incomplete", "insufficiency_without_unresolved_question"}:
        hint = "保留 Q1 原始目标，逐项跟踪用户要求；尚未解决的方面应 blocked 并说明缺失条件，不能用 answered 和证据不足同时结束。"
    elif codes & {"invalid_hypothesis_proposal_target", "invalid_hypothesis_target"}:
        hint = "仅使用本轮成功返回的 Hypothesis proposal_id，或上下文中的正式 Hypothesis ID；不能使用其他运行或其他建议类型。"
    elif "duplicate_reference" in codes:
        hint = "删除指出字段中的重复引用，保留原始有效 ID；不要原样重试。"
    elif "reference_out_of_range" in codes or "reference_not_found" in codes:
        hint = "使用当前案件上下文或检索结果中的有效 ID 和来源索引；不要猜测来源。"
    elif "reference_not_obtained" in codes or "evidence_required" in codes:
        hint = "使用 evidence_context 已提供的有效引用，或用 get_evidence 补充来源后修正参数；探索性假设/缺口可明确无直接证据并使用空列表。"
    elif error["code"] == "duplicate_query":
        hint = "该等价调用未执行；使用已有成功结果或调整参数。失败请求可在获取新节点/来源后修正重试。"
    else:
        hint = "对照工具 Schema 和错误字段修正参数；来源不可用时可换用其他材料或明确报告限制，不要反复原样重试。"
    if name == "finish_investigation":
        hint = "逐项处理 validation_details.errors 的 argument_loc、message 和 fix；" + hint
        if details.get("question_updates_applied"):
            hint += " 合法 Question 更新已保存为工作记忆；question_updates=[] 可复用，只提交需修改的问题。最终答复和 Findings 尚未接受。"
        else:
            hint += " 本次 Question 更新未接受；保留已登记计划 ID 并一次修正所有字段。"
    error["recovery_hint"] = hint
    error["recoverable"] = True


def _validate_summary(text, proposals):
    """Reject report-sized or verbatim Proposal replies; never truncate or rewrite."""
    issues = []

    def reject(code, message, **details):
        issues.append({"loc": ["answer"], "code": code, "message": message, **details})

    if not isinstance(text, str) or not text.strip():
        reject("invalid_summary", "Final reply must be nonblank prose")
        raise AppError("investigation_summary_invalid", "Invalid final reply", details={"category": "summary", "errors": issues})
    length = len(re.sub(r"\s", "", text))
    if length > MAX_SUMMARY_CHARACTERS:
        reject("summary_too_long", "Final reply exceeds the concise summary limit",
               length=length, maximum=MAX_SUMMARY_CHARACTERS)
    try:
        structured = _strict_json(text)
    except (ValueError, TypeError, RecursionError):
        structured = None
    if isinstance(structured, (dict, list)):
        reject("structured_final_reply", "Use artifact tools, not a final JSON result")
    # Ignore punctuation/Markdown/spacing so formatting cannot disguise a full copy.
    normalized = re.sub(r"[\W_]", "", text).casefold()
    for index, proposal in enumerate(proposals):
        for field in ("content", "rationale"):
            value = re.sub(r"[\W_]", "", getattr(proposal, field) or "").casefold()
            # Short factual phrases can legitimately coincide with the answer.
            if len(value) >= 24 and value in normalized:
                reject("proposal_repeated", "Final reply repeats a complete Proposal field",
                       proposal_index=index, field=field)
    if issues:
        raise AppError("investigation_summary_invalid", "Fix all listed reply issues", details={"category": "summary", "errors": issues})
    return text.strip()


async def run_investigation(case: CaseData, goal: str,
                            selected_info_unit_ids: list[str] | None = None, *, work_context: dict | None = None,
                            issue_key: str | None = None) -> dict:
    """Run-local state loop. Only a validated finish call can end normally."""
    from app import investigation_actions as actions
    from app.reasoning_proposals import proposal_receipt
    if not isinstance(goal, str) or not goal.strip() or len(goal) > 2000:
        raise AppError("invalid_investigation_goal", "goal must contain 1 to 2000 nonblank characters")
    bound_case = case.model_copy(deep=True)
    selected = [] if not selected_info_unit_ids else tools._selected_units(bound_case, selected_info_unit_ids)
    counts = Counter(unit.type.value for unit in bound_case.info_units)
    initial_input = {"goal": goal, "core_issue": bound_case.core_issue[:500] if bound_case.core_issue else None,
        "info_unit_type_counts": {kind.value: counts[kind.value] for kind in InfoUnitType},
        "selected_info_units": [tools._summary(unit) for unit in selected]}
    if work_context is not None:
        initial_input["work_context"] = deepcopy(work_context)
    run_id, created_at = f"investigation-{uuid4().hex}", now_iso()
    trace = RunTrace(case_id=bound_case.case_id, case_title=bound_case.case_title, stage="Investigation Agent",
        run_id=run_id, workflow_id=run_id, created_at=created_at, initial_input=initial_input)
    audit = {"run_id": run_id, "run_name": trace.run_name, "stage": "investigation_agent",
        "workflow_id": run_id, "created_at": created_at, "status": "started",
        "model_calls": [], "tool_calls": [], "stop_reason": None, "result": None}
    output_path = f"runs/{run_id}.output.json"
    seen = {"units": {unit.id for unit in selected}, "relations": set(), "evidence": set()}
    canvas = (work_context or {}).get("canvas", {})
    seen["units"].update(row["id"] for row in canvas.get("info_units", []) if row["id"] in {u.id for u in bound_case.info_units})
    seen["relations"].update(r.id for r in bound_case.relations
        if r.id in {row["id"] for row in canvas.get("relations", [])} and {r.source_id, r.target_id} <= seen["units"])
    questions, knowledge = QuestionState(goal), None
    proposals, findings, limitations = [], [], []
    fingerprints, call_ids = {}, set()
    used = {"read": 0, "proposal": 0, "question": 0, "finish": 0}
    limits = {"read": MAX_TOOL_CALLS, "proposal": MAX_PROPOSAL_CALLS,
              "question": MAX_QUESTION_CALLS, "finish": MAX_FINISH_CALLS}
    attempts = executed = no_progress = 0
    stop_reason, recovery = None, None
    completion = "evidence_insufficient"
    recent = []

    def bucket(name):
        return ("read" if name in READ_TOOLS else "proposal" if name in actions.PROPOSAL_TOOLS
                else "question" if name == "update_questions" else "finish" if name == "finish_investigation" else None)

    def snapshot(answer=None, summary_status="pending"):
        result = actions.artifacts(bound_case, seen, work_context, goal, proposals, findings,
                                   questions.open_questions(), completion)
        return {**result.model_dump(mode="json"), "answer": answer, "summary_status": summary_status,
            "question_state": questions.dump(), "limitations": list(dict.fromkeys(limitations)), "run_id": run_id,
            "stop_reason": stop_reason or "running", "tool_call_count": executed,
            "tool_attempt_count": attempts, "tool_budget_used": dict(used), "model_call_count": len(audit["model_calls"])}

    def persist():
        if knowledge is not None:
            knowledge.findings = deepcopy(findings)
            knowledge.proposals = [p.model_dump(mode="json") for p in proposals]
            audit["knowledge_state"] = knowledge.dump()
            audit["investigation_opportunities"] = knowledge.available_work(bound_case, questions)
        audit["question_state"] = questions.dump()
        audit["stop_reason"] = stop_reason
        audit["result"] = snapshot()
        if proposals:
            audit["proposal_receipt"] = proposal_receipt(bound_case, issue_key, work_context, proposals)
        _write_audit(bound_case.case_id, output_path, audit)

    def record_final_state():
        with trace.observation("Investigation Agent · final working state", input={
                "knowledge_state": knowledge.dump() if knowledge else None, "question_state": questions.dump(),
                "investigation_opportunities": audit.get("investigation_opportunities")}) as state_span:
            state_span.update(output={"stop_reason": stop_reason, "completion_status": completion})

    def stopped(reason):
        nonlocal stop_reason, completion
        stop_reason = reason
        completion = "budget_exhausted" if "budget" in reason else "evidence_insufficient"
        limitations.append("连续请求未取得有效进展，本轮已停止；未解决任务保留，可调整问题后继续。"
                           if reason == "no_progress_limit" else "本轮调查预算已用尽；未解决任务保留，可调整问题后继续。")
        persist()
        output = snapshot(None, "failed")
        audit.update(status="partial", result=output)
        _write_audit(bound_case.case_id, output_path, audit)
        record_final_state()
        trace.finish_success(output)
        return output

    try:
        source_ids = list(dict.fromkeys([u.id for u in selected] + [
            row["id"] for row in canvas.get("info_units", []) if row["id"] in seen["units"]]))
        initial_input["evidence_context"] = tools.initial_evidence_context(bound_case, source_ids)
        seen["evidence"].update((row["info_unit_id"], row["source_ref_index"])
                                for row in initial_input["evidence_context"]["items"])
        knowledge = KnowledgeState(initial_input)
        questions.items["Q1"].related_info_unit_ids = source_ids
        questions.items["Q1"].evidence_refs = [EvidencePointer(info_unit_id=uid, source_ref_index=index)
                                             for uid, index in sorted(seen["evidence"])]
        config = llm_client.load_config()
        prompt = load_prompt(PROMPT_PATH, "investigation_agent")
        definitions, metadata = tool_definitions(), prompt.metadata(config.model)
        _write_audit(bound_case.case_id, f"runs/{run_id}.input.json", {
            "run_id": run_id, "model_input": initial_input, "system_prompt": prompt.content,
            "tools": definitions, "configuration": config.public_metadata(), **metadata})
        while True:
            if no_progress >= MAX_NO_PROGRESS:
                return stopped("no_progress_limit")
            if attempts >= MAX_TOTAL_CALLS or used["finish"] >= MAX_FINISH_CALLS:
                return stopped("tool_budget_exhausted")
            if len(audit["model_calls"]) >= MAX_MODEL_CALLS:
                return stopped("model_budget_exhausted")
            available = [d for d in definitions if used[bucket(d["function"]["name"])] < limits[bucket(d["function"]["name"])]]
            remaining = {key: limits[key] - used[key] for key in limits}
            remaining["total"] = MAX_TOTAL_CALLS - attempts
            remaining["model"] = MAX_MODEL_CALLS - len(audit["model_calls"])
            state = model_context(initial_input, knowledge, questions, remaining, recovery)
            request_messages = [{"role": "system", "content": prompt.content},
                                {"role": "user", "content": json.dumps(state, ensure_ascii=False)}, *recent]
            # 'required' is supported by the existing native client; no fabricated tool call/ID for premature prose.
            choice = "required" if recovery and recovery.get("code") == "finish_required" else "auto"
            payload = llm_client.tool_request_payload(config, request_messages, available, tool_choice=choice)
            call_audit = {"step": len(audit["model_calls"]) + 1, **metadata, "request": deepcopy(payload)}
            audit["model_calls"].append(call_audit)
            persist()
            with trace.observation("Investigation Agent · model call", as_type="generation", input=payload,
                    model=config.model, version=prompt.version,
                    model_parameters={"max_completion_tokens": config.max_completion_tokens}, metadata=metadata) as generation:
                reply = await llm_client.call_tools(config, request_messages, available, tool_choice=choice)
                generation.record_reply(reply)
            call_audit.update(raw_response=reply.response, message=deepcopy(reply.message))
            calls = reply.message.get("tool_calls")
            if not calls:
                no_progress += 1
                recovery = {"code": "finish_required", "message": "Ordinary text does not finish investigation. Use finish_investigation with final Question states and findings.",
                            "unaccepted_answer": _preview(reply.content)}
                # Old exchange is retired only after its result has been merged and persisted.
                recent = []
                persist()
                continue
            if not isinstance(calls, list) or len(calls) != 1:
                raise AppError("investigation_protocol_invalid", "Exactly one native tool call is allowed per turn")
            call = calls[0]
            if call["id"] in call_ids:
                raise AppError("investigation_protocol_invalid", "Tool call IDs must be unique within a run")
            call_ids.add(call["id"])
            name, raw = call["function"]["name"], call["function"]["arguments"]
            category = bucket(name)
            attempts += 1
            permitted = category is not None and used[category] < limits[category]
            if permitted:
                used[category] += 1
            tool_audit = {"call_id": call["id"], "name": name, "raw_arguments": raw, "executed": False}
            audit["tool_calls"].append(tool_audit)
            args, qids, finished, fingerprint = {}, [], None, None
            signature = (frozenset(seen["units"]), frozenset(seen["relations"]), frozenset(seen["evidence"]),
                json.dumps(questions.dump()["questions"], sort_keys=True), tuple(p.proposal_id for p in proposals))
            with trace.observation("Investigation Agent · tool call", as_type="tool", input={"name": name, "arguments": raw},
                    metadata={"tool_name": name if category else "unsupported_tool", "attempt": attempts}) as tool_span:
                try:
                    args = _arguments(name, raw)
                    fingerprint = _fingerprint(name, args)
                    tool_audit["arguments"] = deepcopy(args)
                    if not permitted:
                        raise AppError("tool_budget_exhausted", "This tool budget is exhausted; choose a remaining tool or finish")
                    qids = questions.resolve(args.pop("question_ids", None))
                    prior = fingerprints.get(fingerprint)
                    if prior and prior["successful"] and name in READ_TOOLS:
                        response = {**deepcopy(prior["result"]), "cached": True}
                    else:
                        if prior and (prior["successful"] or prior["state"] == signature):
                            raise AppError("duplicate_query", "Equivalent call was already attempted in this working state")
                        tool_audit["executed"] = True
                        executed += 1
                        if name in READ_TOOLS:
                            response = getattr(tools, name)(bound_case, **args)
                            if response["status"] not in {"error", "invalid_request"}:
                                _remember(name, response, seen)
                        elif name in actions.PROPOSAL_TOOLS:
                            draft = list(proposals)
                            response = actions.PROPOSAL_TOOLS[name](bound_case, seen, work_context, draft, **args)
                            actions.artifacts(bound_case, seen, work_context, goal, draft, findings, questions.open_questions(), completion)
                            proposals = draft
                        elif name == "update_questions":
                            response = questions.update(args["questions"], bound_case, seen, work_context, proposals, knowledge)
                        else:
                            finished = actions.finish_investigation(bound_case, seen, work_context, goal, proposals, questions,
                                **args, knowledge=knowledge,
                                budget_exhausted=used["read"] >= limits["read"] or used["proposal"] >= limits["proposal"])
                            result, final_questions, answer = finished
                            findings = [f.model_dump(mode="json") for f in result.findings]
                            questions, completion = final_questions, result.completion_status
                            response = {"status": "ok", "completion_status": completion, "findings": findings}
                    fingerprints[fingerprint] = {"successful": response["status"] not in {"error", "invalid_request"},
                        "state": signature, "result": deepcopy(response)}
                except (AppError, ValidationError) as exc:
                    details = exc.details if isinstance(exc, AppError) else _validation_details(exc)
                    response = tools._failure(exc if isinstance(exc, AppError) else AppError("invalid_tool_arguments", "Fields violate their contract"))
                    response["error"]["validation_details"] = details
                    if fingerprint is not None and response["error"]["code"] != "duplicate_query":
                        fingerprints[fingerprint] = {"successful": False, "state": signature}
                except Exception as exc:
                    tool_audit.update(exception_type=type(exc).__name__, error_code="investigation_tool_failed")
                    response = tools._failure(AppError("investigation_tool_failed", "Tool failed; no artifact was accepted"), status="error")
                    if fingerprint is not None:
                        fingerprints[fingerprint] = {"successful": False, "state": signature}
                _tool_recovery(response, name)
                failed = response["status"] in {"invalid_request", "error"}
                no_progress = no_progress + 1 if failed or response.get("cached") else 0
                recovery = deepcopy(response["error"]) if failed else None
                if response.get("has_more"):
                    limitations.append(f"{name} 返回 truncated；仍有可继续检索的范围，详见工作记忆。")
                # Merge before retiring any protocol messages, including empty/error/truncated activity.
                index = knowledge.merge(name, args, response, qids)
                questions.record(index, qids)
                if not failed and name in READ_TOOLS:
                    obtained_units = list(args.get("info_unit_ids", []))
                    for row in response["items"]:
                        if name == "search_info_units":
                            obtained_units.append(row["id"])
                        elif name == "get_relations":
                            obtained_units.extend((row["relation"]["source_id"], row["relation"]["target_id"]))
                    for qid in qids:
                        q = questions.items[qid]
                        for uid in obtained_units:
                            if uid not in q.related_info_unit_ids:
                                q.related_info_unit_ids.append(uid)
                        if name == "get_evidence":
                            for row in response["items"]:
                                pointer = EvidencePointer(info_unit_id=row["info_unit_id"], source_ref_index=row["source_ref_index"])
                                if pointer not in q.evidence_refs:
                                    q.evidence_refs.append(pointer)
                    if response.get("cached") and json.dumps(questions.dump()["questions"], sort_keys=True) != signature[3]:
                        no_progress = 0  # Reusing a source for a new Question is useful progress.
                tool_audit["question_ids"] = qids
                tool_audit["result"] = deepcopy(response)
                tool_span.update(output=response)
            persist()
            if finished:
                stop_reason = "finish_investigation"
                output = snapshot(answer, "completed")
                audit.update(status="success", stop_reason=stop_reason, result=output)
                _write_audit(bound_case.case_id, output_path, audit)
                record_final_state()
                trace.finish_success(output)
                return output
            # Keep exactly the latest completed Assistant/Tool pair, never an unpaired call.
            recent = [deepcopy(reply.message), {"role": "tool", "tool_call_id": call["id"],
                "content": json.dumps(response if len(json.dumps(response, ensure_ascii=False)) <= 12000 else _preview(response, 240), ensure_ascii=False)}]
            if len(json.dumps(recent, ensure_ascii=False)) > 16000:
                recent = []  # Already merged/persisted; retire the entire pair together.
    except Exception as exc:
        error = trace.error_payload(exc)
        stop_reason = "model_failed" if isinstance(exc, llm_client.LLMFailure) else "error"
        persist()
        partial = isinstance(exc, (llm_client.LLMFailure, AppError)) and bool(proposals or findings) and getattr(exc, "code", None) != "investigation_audit_failed"
        audit.update(status="partial" if partial else "failed", error=error, stop_reason=stop_reason,
                     result=snapshot(None, "failed"))
        if isinstance(exc, llm_client.LLMFailure):
            audit["provider_response"] = exc.response
        record_final_state()
        trace.finish_error(exc)
        if partial:
            output = snapshot(None, "failed")
            audit["result"] = output
            _write_audit(bound_case.case_id, output_path, audit)
            return output
        _write_audit(bound_case.case_id, output_path, audit)
        if isinstance(exc, AppError):
            exc.details = {**exc.details, "run_id": run_id}
        raise
