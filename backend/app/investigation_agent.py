"""Finite, case-bound investigation through native read-only function calls."""
from __future__ import annotations

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
from app.reasoning_models import (ProposalEvidenceRef, ReasoningProposal, MaterialProposal,
                                  EvidenceGapProposal, EvidenceGapUpdateProposal, proposal_material_ids)
from app.investigation_state import KnowledgeState, case_info_directory, model_context

MAX_NON_FINISH_TOOL_CALLS = 8  # All read/Proposal attempts, including failures.
MAX_FINISH_CALLS = 3
MAX_TOTAL_CALLS = MAX_NON_FINISH_TOOL_CALLS + MAX_FINISH_CALLS
MAX_MODEL_CALLS = MAX_TOTAL_CALLS + 3
MAX_SUMMARY_CHARACTERS = 1000  # Same bound in the native schema and backend validation.
MAX_NO_PROGRESS = 3
READ_TOOLS = {"search_info_units": tools.search_info_units, "get_relations": tools.get_relations,
              "get_evidence": tools.get_evidence, "search_evidence_gaps": tools.search_evidence_gaps}
PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts/investigation_common_prompt.md"
PROMPT_NAME = "investigation_common"
BoundedText = Annotated[str, Field(min_length=1, max_length=600, pattern=r"\S")]
IDs = Annotated[list[BoundedText], Field(max_length=80)]


class InvestigationOutput(BaseModel):
    """Transport-only contracts; the persistent CaseData Schema is unchanged."""
    model_config = ConfigDict(extra="forbid", strict=True)


Pointers = Annotated[list[ProposalEvidenceRef], Field(max_length=64)]


class InvestigationResult(InvestigationOutput):
    goal: Annotated[str, Field(min_length=1, max_length=2000, pattern=r"\S")]
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
        _function("search_info_units", (
            "Search current-case titles and content for one specific information need; keywords use OR matching. "
            "Use known directory titles to inspect relevant existing material before declaring it missing. "
            "Optional types filter results; limit defaults to 5, maximum 8. Returns summaries of up to 120 characters with attribution. "
            "Excludes InfoUnits visible in the current material view; omitted items can be retrieved again. "
            "has_more means matching items remain, not that all must be read."
        ), {
            "query": {"type": "string", "minLength": 1, "maxLength": tools.MAX_QUERY_CHARS},
            "types": {"type": ["array", "null"], "items": {"type": "string", "enum": [t.value for t in InfoUnitType]}},
            "limit": limit,
        }),
        _function("get_relations", (
            "Read persisted one-hop relations for 1-5 current-case info_unit_ids when connections matter to the Goal. "
            "Optional relation_types filter results; limit defaults to 5, maximum 8. "
            "Returns directed relations and adjacent InfoUnit summaries without recursive expansion; has_more marks omitted matches."
        ), {
            "info_unit_ids": ids,
            "relation_types": {"type": ["array", "null"], "items": {"type": "string", "enum": [t.value for t in RelationType]}},
            "limit": limit,
        }),
        _function("get_evidence", (
            "Read cached source quotes for one info_unit_id to check wording, dates, amounts, actors or source qualifications; "
            "not every InfoUnit needs a source read. offset is the original SourceRef list position, default 0; limit defaults to 12, maximum 12. "
            "Returns full quotes verified against cached TXT, at most 8000 characters total, with stable source_ref_index values usable in Proposals. "
            "total_count counts the node's references; when has_more is true, continue with next_offset for the same node."
        ), {
            "info_unit_id": {"type": "string", "minLength": 1},
            "offset": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": tools.MAX_EVIDENCE_REFS},
        }),
        _function("search_evidence_gaps", (
            "Discover existing Gaps in the current Issue, including those omitted from the initial reasoning view, before creating or updating a Gap. "
            "query uses OR keywords on questions and linked Hypotheses; info_unit_ids match basis, addressing material and Hypothesis evidence links. "
            "Leave both empty for bounded browsing. limit defaults to 3, maximum 3; offset defaults to 0 and can advance when has_more is true. "
            "Returns actual IDs, full questions and bounded linked reasoning/status. Reference lists do not grant material or source access."
        ), {
            "query": {"type": ["string", "null"], "maxLength": tools.MAX_QUERY_CHARS},
            "info_unit_ids": {"type": ["array", "null"], "items": {"type": "string"}, "maxItems": tools.MAX_NODE_IDS},
            "limit": {"type": ["integer", "null"], "minimum": 1, "maximum": 3},
            "offset": {"type": ["integer", "null"], "minimum": 0},
        }),
    ]
    from app.investigation_actions import PROPOSAL_TOOLS, finish_investigation
    schema = MaterialProposal.model_json_schema()
    props = schema["properties"]
    common = {key: props[key] for key in ("rationale", "info_unit_ids", "evidence_refs")}
    grounded = {**common, "info_unit_ids": {**props["info_unit_ids"], "minItems": 1},
                "evidence_refs": {**props["evidence_refs"], "minItems": 1}}
    content = next(part for part in props["content"]["anyOf"] if part.get("type") != "null")
    text_id = {"type": "string", "minLength": 1, "pattern": r"\S"}
    # A nested union expresses the cross-field requirement within the native
    # strict schema: the root remains an object, and neither branch permits
    # two empty association lists. Domain Proposal fields remain unchanged.
    gap_targets = {"anyOf": [
        {"type": "object", "properties": {
            "hypothesis_ids": {**props["hypothesis_ids"], **({"minItems": 1} if field == "hypothesis_ids" else {})},
            "hypothesis_proposal_ids": {**props["hypothesis_proposal_ids"], **({"minItems": 1} if field == "hypothesis_proposal_ids" else {})}},
         "required": ["hypothesis_ids", "hypothesis_proposal_ids"], "additionalProperties": False}
        for field in ("hypothesis_ids", "hypothesis_proposal_ids")]}
    specifications = {
        "propose_hypothesis": {**common, "content": content},
        "propose_evidence_link": {"hypothesis_id": {"anyOf": [text_id, {"type": "null"}]}, "info_unit_id": text_id,
            "hypothesis_proposal_ids": {**props["hypothesis_proposal_ids"], "maxItems": 1},
            "meaning": next(p for p in props["meaning"]["anyOf"] if p.get("type") != "null"),
            "rationale": props["rationale"], "evidence_refs": grounded["evidence_refs"]},
        "propose_evidence_gap": {"rationale": props["rationale"], "evidence_refs": props["evidence_refs"],
            "basis_info_unit_ids": EvidenceGapProposal.model_json_schema()["properties"]["basis_info_unit_ids"],
            "content": content, "hypothesis_targets": gap_targets},
        "propose_hypothesis_status": {**grounded, "hypothesis_id": text_id,
            "judgment": next(p for p in props["judgment"]["anyOf"] if p.get("type") != "null")},
        "propose_evidence_gap_update": {"gap_id": text_id,
            "addressing_info_unit_ids": EvidenceGapUpdateProposal.model_json_schema()["properties"]["addressing_info_unit_ids"],
            "status": {"type": "string", "enum": ["open", "resolved"]},
            "rationale": props["rationale"], "evidence_refs": grounded["evidence_refs"]},
    }
    for name, properties in specifications.items():
        definition = _function(name, PROPOSAL_TOOLS[name].__doc__, deepcopy(properties))
        definition["function"]["parameters"]["$defs"] = schema["$defs"]
        definitions.append(definition)
    definition = _function("finish_investigation", finish_investigation.__doc__, {
        "answer": {"type": "string", "minLength": 1, "maxLength": MAX_SUMMARY_CHARACTERS},
        "answer_basis": {"type": "string", "enum": ["general", "case_metadata", "case_material"]},
        "open_questions": InvestigationResult.model_json_schema()["properties"]["open_questions"],
        "completion_status": InvestigationResult.model_json_schema()["properties"]["completion_status"],
    })
    definitions.append(definition)
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
    if len(raw) > (4000 if name in READ_TOOLS else 32000 if name == "finish_investigation" else 12000):
        raise AppError("invalid_tool_arguments", "Tool arguments exceed their bounded size")
    try:
        args = _strict_json(raw)
    except (ValueError, TypeError, RecursionError) as exc:
        raise AppError("invalid_tool_arguments", "Tool arguments must be strict JSON", details={
            "category": "json", "errors": [{"loc": [], "code": "invalid_json",
                **{key: getattr(exc, key) for key in ("lineno", "colno", "pos") if hasattr(exc, key)}}]}) from exc
    fields = {d["function"]["name"]: (set(d["function"]["parameters"]["required"]),
              set(d["function"]["parameters"]["properties"])) for d in tool_definitions()}
    fields["get_evidence"] = ({"info_unit_id"}, fields["get_evidence"][1])
    fields["search_info_units"] = ({"query"}, fields["search_info_units"][1])
    fields["get_relations"] = ({"info_unit_ids"}, fields["get_relations"][1])
    # Existing formal-Hypothesis calls remain valid; native schemas ask for an explicit empty list.
    if "propose_evidence_link" in fields:
        fields["propose_evidence_link"] = (fields["propose_evidence_link"][0] - {"hypothesis_proposal_ids"}, fields["propose_evidence_link"][1])
    if name not in fields:
        raise AppError("unknown_tool", "Only registered investigation and pending-artifact tools are allowed")
    required, allowed = fields[name]
    if not isinstance(args, dict) or not required <= args.keys() or not args.keys() <= allowed:
        missing = sorted(required - args.keys()) if isinstance(args, dict) else []
        errors = [{"loc": [field], "code": "missing", "message": "Required tool argument is missing",
                   "fix": "Supply the field as described in the tool schema"} for field in missing]
        if isinstance(args, dict):
            errors.extend({"loc": [field], "code": "extra_forbidden",
                           "message": "Unsupported tool argument; remove this field", "fix": "Remove this field and resubmit"}
                          for field in sorted(args.keys() - allowed))
        else:
            errors.append({"loc": [], "code": "invalid_arguments", "message": "Arguments must be a JSON object",
                           "fix": "Submit a JSON object containing the tool's required fields"})
        raise AppError("invalid_tool_arguments", "Missing or unsupported tool argument fields", details={
            "category": "structure", "errors": errors})
    if name in {"search_info_units", "get_relations"}:
        args["limit"] = tools.DEFAULT_LIMIT if args.get("limit") is None else args["limit"]
        args.setdefault("types" if name == "search_info_units" else "relation_types", None)
    if name == "get_evidence":
        args.setdefault("offset", 0)
        args.setdefault("limit", tools.MAX_EVIDENCE_REFS)
    if name == "propose_evidence_link":
        args.setdefault("hypothesis_proposal_ids", [])
    if name == "propose_evidence_gap":
        targets = args["hypothesis_targets"]
        fields = {"hypothesis_ids", "hypothesis_proposal_ids"}
        if (not isinstance(targets, dict) or set(targets) != fields
                or any(not isinstance(targets[field], list) for field in fields)
                or not any(targets.values())):
            raise AppError("invalid_tool_arguments", "A Gap must be associated with a Hypothesis", details={
                "category": "structure", "errors": [{"loc": ["hypothesis_targets"], "code": "hypothesis_target_required",
                    "message": "hypothesis_targets must contain both ID arrays and at least one actual target",
                    "fix": "Use current reasoning Hypothesis IDs in hypothesis_ids, or first call propose_hypothesis and put its successfully returned proposal_id in hypothesis_proposal_ids; never send two empty lists."}]})
    return args


def _fingerprint(name: str, args: dict, *, visible_info_unit_ids=frozenset()) -> str:
    normalized = deepcopy(args)
    if name == "search_info_units" and isinstance(args.get("query"), str):
        normalized["query"] = sorted(set(re.findall(r"\w+", tools._normalized(args["query"]))))
    if name == "search_info_units" and normalized.get("types") is None:
        normalized["types"] = [kind.value for kind in InfoUnitType]
    if name == "search_info_units":
        # Search results depend on current visibility, including on cache hits.
        # An omitted old node may be read again; a visible cached hit is excluded.
        normalized["exclude_info_unit_ids"] = sorted(visible_info_unit_ids)
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


def _material_keys(materials: dict) -> set[tuple[str, str]]:
    """Compare material identity, not ordering, notices or acquisition counters."""
    return {(kind, KnowledgeState.ref_key(row) if kind == "source_refs" else row["id"])
            for kind, section in materials.items() for row in section["items"]}


def _context_keys(state: dict) -> set[tuple[str, str]]:
    return _material_keys(state["materials"]) | {
        (kind, row["id"]) for kind in ("hypotheses", "gaps", "links") for row in state["reasoning"][kind]}


def _include_gap_targets(context, formal, gap_ids):
    """Capture existing targets for validation/adoption, outside the model view."""
    if context is None:
        return
    gaps = [g for g in formal.gaps if g.id in gap_ids]
    hids = {hid for g in gaps for hid in g.hypothesis_ids}
    for kind, objects in (("gaps", gaps), ("hypotheses", [h for h in formal.hypotheses if h.id in hids])):
        rows = {row["id"]: row for row in context.get("reasoning", {}).get(kind, [])}
        rows.update((obj.id, obj.model_dump(mode="json")) for obj in objects)
        context.setdefault("reasoning", {})[kind] = list(rows.values())


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
        code = error["type"]
        if "Duplicate" in error["msg"]:
            code = "duplicate_reference"
            loc += [next((field for field in ("hypothesis_proposal_ids", "hypothesis_proposal_indices", "basis_info_unit_ids", "info_unit_ids", "hypothesis_ids")
                          if field in error["msg"]), "references")]
        if "Grounded proposals require" in error["msg"]:
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

    def invalid(code, category, loc, message, detail_code=None, *, value=None, kind="unit", fix=None):
        item = {"loc": loc, "code": detail_code or code, "message": message,
                "category": category, "error_code": code}
        if value is not None:
            uid = value[0] if isinstance(value, tuple) else value
            if kind == "unit":
                item["info_unit_id"] = uid
                if uid in units:
                    item["info_unit_title"] = units[uid].title
                if isinstance(value, tuple):
                    item["source_ref_index"] = value[1]
                item["message"] += f": {uid}" + (f" ({units[uid].title})" if uid in units else "")
            else:
                item[f"{kind}_id"] = uid
        item["fix"] = fix or (
            "Use a real Hypothesis ID from the current Issue or a successfully returned same-run Hypothesis Proposal ID."
            if kind == "hypothesis" else
            "Use the current Issue's actual Gap ID." if kind == "gap" else
            "Read get_relations for obtained InfoUnits and use a returned Relation ID." if kind == "relation" else
            "Call get_evidence for this InfoUnit and use its returned SourceRef index, then resubmit."
            if isinstance(value, tuple) else
            "Call search_info_units with the indicated title or keywords to obtain this InfoUnit; call get_evidence if original source text is needed, then resubmit.")
        issues.append(item)

    def check(ids, available, loc, known=None, kind="unit"):
        visited = set()
        for index, value in enumerate(ids):
            detail = ("duplicate_reference" if value in visited else "reference_not_found" if known is not None and value not in known
                      else "reference_not_obtained" if value not in available else None)
            if detail:
                invalid("invalid_investigation_reference", "reference", [*loc, index],
                              "Duplicate reference" if detail == "duplicate_reference" else
                              "Reference does not exist in the current case/Issue" if detail == "reference_not_found" else
                              "Reference has not been obtained or included in the artifact basis", detail,
                              value=value, kind=kind)
            visited.add(value)

    check(result.relevant_info_unit_ids, seen["units"] & units.keys(), ["relevant_info_unit_ids"], units)
    check(result.relevant_relation_ids, seen["relations"] & relations, ["relevant_relation_ids"], relations, "relation")
    pointers = [(ref.info_unit_id, ref.source_ref_index) for ref in result.evidence_refs]
    for offset, (uid, index) in enumerate(pointers):
        if uid not in units or index >= len(units[uid].source_refs) or uid not in result.relevant_info_unit_ids:
            invalid("invalid_investigation_reference", "reference", ["evidence_refs", offset], "Invalid SourceRef index or InfoUnit",
                          "reference_not_found" if uid not in units else "reference_out_of_range" if index >= len(units[uid].source_refs) else "reference_not_obtained",
                          value=(uid, index))
    check(pointers, seen["evidence"], ["evidence_refs"])
    if result.goal != goal:
        invalid("investigation_result_invalid", "result_semantics", ["goal"], "Final goal must match the requested goal")
    if result.completion_status == "unsupported" and (
            result.relevant_info_unit_ids or result.relevant_relation_ids
            or result.evidence_refs or result.proposals or not result.open_questions):
        invalid("investigation_result_invalid", "result_semantics", ["completion_status"], "Unsupported requests require an explanation and no material-backed artifacts")
    targets = {obj["id"] for kind in ("hypotheses", "gaps")
               for obj in (work_context or {}).get("reasoning", {}).get(kind, [])}
    hypothesis_ids = {obj["id"] for obj in (work_context or {}).get("reasoning", {}).get("hypotheses", [])}
    gap_ids = {obj["id"] for obj in (work_context or {}).get("reasoning", {}).get("gaps", [])}
    from app.reasoning_proposals import validate_proposal_dependencies
    try:
        validate_proposal_dependencies(result.proposals)
    except AppError as exc:
        from app.investigation_state import validation_issues
        issues.extend(validation_issues(exc))
    for offset, proposal in enumerate(result.proposals):
        if not work_context:
            invalid("invalid_reasoning_proposal", "proposal_semantics", ["proposals", offset], "Proposals require a bound Issue")
        material_field = ("basis_info_unit_ids" if proposal.kind == "evidence_gap" else
                          "addressing_info_unit_ids" if proposal.kind == "evidence_gap_update" else "info_unit_ids")
        check(proposal_material_ids(proposal), set(result.relevant_info_unit_ids), ["proposals", offset, material_field])
        check(proposal.hypothesis_ids, hypothesis_ids & targets, ["proposals", offset, "hypothesis_ids"], hypothesis_ids, "hypothesis")
        if proposal.kind == "evidence_gap_update":
            check([proposal.gap_id], gap_ids, ["proposals", offset, "gap_id"], gap_ids, "gap")
        check([(p.info_unit_id, p.source_ref_index) for p in proposal.evidence_refs], set(pointers), ["proposals", offset, "evidence_refs"])
        for ref_index, ref in enumerate(proposal.evidence_refs):
            if ref.info_unit_id not in proposal_material_ids(proposal):
                invalid("invalid_reasoning_proposal", "reference", ["proposals", offset, "evidence_refs", ref_index],
                    "Proposal citations must refer to its own material IDs", "proposal_evidence_outside_materials",
                    value=(ref.info_unit_id, ref.source_ref_index),
                    fix=f"Cite only {material_field}: {proposal_material_ids(proposal)}. Gap updates cite responding material, not the original Gap basis.")
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


def _tool_recovery(response, name, args=None, case=None):
    """Add actionable diagnostics to this loop's existing tool error envelope."""
    if response["status"] not in {"error", "invalid_request"}:
        return
    error = response["error"]
    args = args or {}
    details = error.setdefault("validation_details", {})
    if not details:
        details.update(category="tool", errors=[{"loc": [], "code": error["code"]}])
        if error["code"] == "unknown_info_unit":
            known = {u.id for u in case.info_units} if case else set()
            values = [args.get("info_unit_id")] if name == "get_evidence" else args.get("info_unit_ids", [])
            details.update(category="reference", errors=[{"loc": ["info_unit_id"] if name == "get_evidence" else ["info_unit_ids", index],
                "code": "reference_not_found", "info_unit_id": uid}
                for index, uid in enumerate(values) if uid not in known] or
                [{"loc": ["info_unit_ids"], "code": "reference_not_found"}])
        elif error["code"] in {"invalid_limit", "invalid_filter", "invalid_info_unit_ids", "invalid_info_unit_id", "invalid_offset", "invalid_query"}:
            field = ("limit" if error["code"] == "invalid_limit" else
                     "offset" if error["code"] == "invalid_offset" else
                     "info_unit_id" if error["code"] == "invalid_info_unit_id" else
                     "types" if error["code"] == "invalid_filter" and name == "search_info_units" else
                     "relation_types" if error["code"] == "invalid_filter" else
                     "query" if error["code"] == "invalid_query" else "info_unit_ids")
            details["errors"][0]["loc"] = [field]
    for item in details["errors"]:
        loc = item.get("loc", [])
        argument_loc = loc[2:] if loc[:1] == ["proposals"] else list(loc)
        # Discriminated-union tags describe a variant, not a native argument.
        if argument_loc[:1] in (["hypothesis"], ["evidence_gap"], ["evidence_link"], ["hypothesis_status"], ["evidence_gap_update"]):
            argument_loc = argument_loc[1:]
        if argument_loc[:1] == ["relevant_info_unit_ids"]:
            argument_loc[0] = "info_unit_ids"
        if name == "propose_evidence_link" and argument_loc[:1] in (["info_unit_ids"], ["hypothesis_ids"]):
            argument_loc = ["info_unit_id" if argument_loc[0] == "info_unit_ids" else "hypothesis_id"]
        if name == "propose_evidence_gap" and argument_loc[:1] in (["hypothesis_ids"], ["hypothesis_proposal_ids"]):
            argument_loc = ["hypothesis_targets", *argument_loc]
        item.setdefault("argument_loc", argument_loc)
        if "info_unit_id" not in item:
            value = args
            try:
                for part in argument_loc:
                    value = value[part]
            except (KeyError, IndexError, TypeError):
                value = None
            if isinstance(value, dict) and isinstance(value.get("info_unit_id"), str):
                item["info_unit_id"] = value["info_unit_id"]
                if "source_ref_index" in value:
                    item["source_ref_index"] = value["source_ref_index"]
            elif isinstance(value, str) and argument_loc and argument_loc[0] in {"info_unit_id", "info_unit_ids", "basis_info_unit_ids", "addressing_info_unit_ids"}:
                item["info_unit_id"] = value
        if case and item.get("info_unit_id"):
            unit = next((u for u in case.info_units if u.id == item["info_unit_id"]), None)
            if unit:
                item.setdefault("info_unit_title", unit.title)
        if item["code"] in {"reference_not_obtained", "reference_not_found"} and (
                argument_loc[:1] in (["hypothesis_ids"], ["hypothesis_id"]) or argument_loc[:2] == ["hypothesis_targets", "hypothesis_ids"]):
            item["code"] = "invalid_hypothesis_target"
        item.setdefault("message", error.get("message", item["code"]))
        code = item["code"]
        uid, title = item.get("info_unit_id"), item.get("info_unit_title")
        target = {key: item[key] for key in ("info_unit_id", "info_unit_title", "source_ref_index", "relation_id", "hypothesis_id", "gap_id") if key in item}
        step = {"loc": argument_loc, "code": code, **target}
        if code == "proposal_evidence_outside_materials":
            ids = args.get("addressing_info_unit_ids", args.get("basis_info_unit_ids", args.get("info_unit_ids", [])))
            step.update(tool=name, instruction=item["fix"], material_ids=ids)
        elif code == "reference_not_found":
            step.update(tool="search_info_units", instruction="Search the current case for valid IDs by title/keywords; never guess an ID. Use returned materials before resubmitting.")
        elif code == "reference_out_of_range" or code == "evidence_required" or code == "reference_not_obtained" and "source_ref_index" in item:
            step.update(tool="get_evidence", instruction="Actually read the indicated source with get_evidence; use only successfully returned SourceRef indices and resubmit the corrected tool call.")
            if uid:
                step["arguments"] = {"info_unit_id": uid,
                    "offset": 0 if code == "reference_out_of_range" else item.get("source_ref_index", 0), "limit": tools.MAX_EVIDENCE_REFS}
            elif code == "evidence_required":
                ids = args.get("info_unit_ids") or ([args["info_unit_id"]] if args.get("info_unit_id") else args.get("addressing_info_unit_ids"))
                if ids:
                    step["arguments"] = {"info_unit_id": ids[0], "offset": 0, "limit": tools.MAX_EVIDENCE_REFS}
                    step["targets"] = [{"info_unit_id": uid, "info_unit_title": u.title}
                        for uid in ids for u in (case.info_units if case else []) if u.id == uid]
        elif code == "reference_not_obtained":
            if "relation_id" in item:
                step.update(tool="get_relations", instruction="Read relations for obtained InfoUnits; use a returned Relation ID and resubmit.")
            else:
                step.update(tool="search_info_units", instruction="Actually search by the missing InfoUnit title or keywords to obtain its content; if original text is required call get_evidence next, then resubmit. Directory titles alone do not grant evidence.")
                if title:
                    step["arguments"] = {"query": title}
        elif code == "hypothesis_target_required":
            step.update(tool=name, instruction=item["fix"])
        elif code == "duplicate_proposal":
            step.update(existing_object=deepcopy(item["existing_object"]),
                instruction="Reuse the identified formal object or successful pending Proposal and its actual ID; do not create a synonym. For a genuinely competing Hypothesis, state its different condition or distinguishing evidence explicitly. One shared Gap should target all relevant Hypotheses; do not recreate it for each target. No object was modified or adopted.")
        elif code in {"invalid_hypothesis_target", "invalid_hypothesis_proposal_target"}:
            step.update(tool=name, instruction="Use a real Hypothesis ID from the current Issue, or a Hypothesis Proposal ID successfully returned in this run in the appropriate dependency field, then resubmit.")
        elif code == "duplicate_query":
            step.update(instruction="Reuse existing results; do not repeat the identical call. Correct failed parameters only after obtaining new relevant material, or finish with the limitation.")
        elif code in {"tool_budget_exhausted", "no_progress_limit", "finish_required"}:
            step.update(tool="finish_investigation", instruction="Stop further investigation. Call finish_investigation using only obtained material and state the limitation in answer/open_questions. Use budget_exhausted only when the shared eight non-Finish attempts are spent.")
        elif code == "investigation_tool_failed":
            step.update(instruction="Do not unconditionally repeat the failed call. Try other available material or call finish_investigation explicitly describing the unavailable source and limitation.")
        elif code == "initial_information_required":
            step.update(tool="search_info_units", instruction="Read relevant material by title/keywords before making a case judgment; directory metadata is not obtained evidence.")
        else:
            step.update(tool=name, instruction=item.get("fix") or item["message"] + "; correct this field according to the tool schema and resubmit.")
        item["fix"] = step["instruction"]
        item["next_action"] = step
    codes = {item["code"] for item in details["errors"]}
    if "initial_information_required" in codes:
        hint = "Read relevant case material with the read tools; directory entries do not provide evidence."
    elif codes & {"invalid_hypothesis_proposal_target", "invalid_hypothesis_target"}:
        hint = "仅使用本轮成功返回的 Hypothesis proposal_id，或上下文中的正式 Hypothesis ID；不能使用其他运行或其他建议类型。"
    elif "duplicate_reference" in codes:
        hint = "删除指出字段中的重复引用，保留原始有效 ID；不要原样重试。"
    elif "reference_out_of_range" in codes or "reference_not_found" in codes:
        hint = "ID 不存在时使用当前案件检索工具取得有效 ID；来源下标越界时对对应 InfoUnit 调用 get_evidence，使用返回的有效索引。不得猜测。"
    elif "reference_not_obtained" in codes or "evidence_required" in codes:
        hint = "按错误中的标题或关键词调用 search_info_units 实际取得缺失 InfoUnit；需要原文或来源引用时调用 get_evidence，成功后修正并重新提交，不得只删除引用重复同一结论。"
    elif error["code"] == "duplicate_query":
        hint = "该等价调用未执行；使用已有成功结果或调整参数。失败请求可在获取新节点/来源后修正重试。"
    elif error["code"] in {"tool_budget_exhausted", "no_progress_limit"}:
        hint = "停止调查，使用当前已取得的信息调用 finish_investigation 并如实说明限制。"
    elif error["code"] == "investigation_tool_failed":
        hint = "不要无条件重复相同失败调用；尝试其他可用材料，或通过 finish_investigation 明确说明来源不可用的限制。"
    else:
        hint = "对照工具 Schema 和错误字段修正参数；来源不可用时可换用其他材料或明确报告限制，不要反复原样重试。"
    if name == "finish_investigation":
        hint = "逐项处理 validation_details.errors 的 argument_loc、message 和 fix；" + hint
    error["recovery_hint"] = hint
    error["next_action"] = {"steps": [item["next_action"] for item in details["errors"]], "retry_tool": name}
    error["recoverable"] = True


def _finish_only_recovery(recovery):
    """Constrain executable recovery actions without losing validation diagnostics."""
    recovery["constraints"] = {"allowed_tools": ["finish_investigation"],
        "instruction": "Investigation tools are closed. Only finish_investigation is available; correct its listed errors using obtained material, without further reads or Proposals."}

    def constrain(step):
        if step.get("tool") in {None, "finish_investigation"}:
            return step
        step = deepcopy(step)
        step["unavailable_tool"] = step["tool"]
        if "arguments" in step:
            step["unavailable_arguments"] = step.pop("arguments")
        step["tool"] = "finish_investigation"
        step["instruction"] = (f"Correct {step.get('loc', [])} ({step.get('code', 'tool_error')}) in finish_investigation "
            "using only already obtained material. "
            "Do not call the unavailable tool or invent evidence. Describe unresolved conditions in open_questions, then resubmit Finish.")
        return step

    action = recovery.setdefault("next_action", {"steps": []})
    action["steps"] = [constrain(step) for step in action["steps"]]
    action["retry_tool"] = "finish_investigation"
    for error in recovery.get("validation_details", {}).get("errors", []):
        if "next_action" in error:
            revised = constrain(error["next_action"])
            if revised is not error["next_action"]:
                error.setdefault("original_fix", error["fix"])
                error["fix"] = revised["instruction"]
                error["next_action"] = revised
    if any("unavailable_tool" in step for step in action["steps"]):
        recovery.setdefault("original_recovery_hint", recovery.get("recovery_hint"))
        recovery["recovery_hint"] = recovery["constraints"]["instruction"] + " Follow the field-specific next_action steps."


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
    """One goal-driven loop. Task plans and activity history are not model input."""
    from app import investigation_actions as actions
    from app.reasoning_proposals import proposal_receipt, commit_investigation
    if not isinstance(goal, str) or not goal.strip() or len(goal) > 2000:
        raise AppError("invalid_investigation_goal", "goal must contain 1 to 2000 nonblank characters")
    bound_case = case.model_copy(deep=True)
    work_context = deepcopy(work_context)
    from app.issue_workspaces import get_issue_workspaces, investigation_context
    from app.reasoning_models import ReasoningState
    # The formal Issue snapshot governs availability, independently of its
    # model projection. It stays on the server; discovery is a bounded read.
    formal = ReasoningState()
    if issue_key is not None:
        workspace = get_issue_workspaces(bound_case).workspaces.get(issue_key)
        if workspace is None:
            raise AppError("unknown_issue", "Issue does not belong to the current case")
        formal = workspace.reasoning
        if work_context is None:
            work_context = investigation_context(bound_case, issue_key, goal, selected_info_unit_ids=selected_info_unit_ids)
        elif work_context.get("current_issue", issue_key) != issue_key:
            raise AppError("unknown_issue", "Investigation context belongs to another Issue")
        work_context["current_issue"] = issue_key
    selected = [] if not selected_info_unit_ids else tools._selected_units(bound_case, selected_info_unit_ids)
    initial_input = {"goal": goal, "core_issue": bound_case.core_issue,
        "case_info_directory": case_info_directory(bound_case),
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
    knowledge = None
    proposals, open_questions, limitations = [], [], []
    fingerprints, call_ids = {}, set()
    used = {"read": 0, "proposal": 0, "finish": 0}
    attempts = executed = no_progress = 0
    stop_reason, recovery, answer_basis = None, None, None
    finish_required = False
    completion = "evidence_insufficient"
    workspace_commit = {"status": "pending", "changes": []}

    def bucket(name):
        return ("read" if name in READ_TOOLS else "proposal" if name in actions.PROPOSAL_TOOLS
                else "finish" if name == "finish_investigation" else None)

    def non_finish_exhausted():
        return used["read"] + used["proposal"] >= MAX_NON_FINISH_TOOL_CALLS

    def investigation_closed():
        return finish_required or non_finish_exhausted() or no_progress >= MAX_NO_PROGRESS

    def snapshot(answer=None, summary_status="pending"):
        result = actions.artifacts(bound_case, seen, work_context, goal, proposals, open_questions, completion).model_dump(mode="json")
        if answer_basis not in {"general", "case_metadata"} and completion != "unsupported":
            # Provenance records obtained material, without asserting support for the answer.
            result.update(relevant_info_unit_ids=sorted(seen["units"]), relevant_relation_ids=sorted(seen["relations"]),
                evidence_refs=[{"info_unit_id": uid, "source_ref_index": index} for uid, index in sorted(seen["evidence"])])
        return {**result, "answer": answer, "answer_basis": answer_basis,
            "summary_status": summary_status, "limitations": list(dict.fromkeys(limitations)), "run_id": run_id,
            "workspace_commit": workspace_commit,
            "stop_reason": stop_reason or "running", "tool_call_count": executed,
            "tool_attempt_count": attempts, "tool_budget_used": dict(used), "model_call_count": len(audit["model_calls"])}

    def persist():
        if knowledge is not None:
            knowledge.proposals = [p.model_dump(mode="json") for p in proposals]
            audit["knowledge_state"] = knowledge.dump()
            audit["information_acquisition"] = knowledge.acquisition()
        audit["stop_reason"] = stop_reason
        audit["result"] = snapshot()
        if proposals:
            audit["proposal_receipt"] = proposal_receipt(bound_case, issue_key, proposals, formal)
        _write_audit(bound_case.case_id, output_path, audit)

    def record_final_state():
        with trace.observation("Investigation Agent · final working state", input={
                "goal": goal, "knowledge_state": knowledge.dump() if knowledge else None}) as span:
            span.update(output={"stop_reason": stop_reason, "completion_status": completion},
                metadata={"information_acquisition": audit.get("information_acquisition")})

    def stopped(reason):
        nonlocal stop_reason, completion
        stop_reason = reason
        completion = "budget_exhausted" if non_finish_exhausted() else "evidence_insufficient"
        limitations.append("Execution stopped without a validated answer: " + reason)
        persist()
        output = snapshot(None, "failed")
        audit.update(status="partial", result=output)
        _write_audit(bound_case.case_id, output_path, audit)
        record_final_state()
        trace.finish_success(output)
        return output

    try:
        knowledge = KnowledgeState(initial_input)
        config = llm_client.load_config()
        prompt = load_prompt(PROMPT_PATH, PROMPT_NAME)
        metadata = prompt.metadata(config.model)
        audit["prompt"] = metadata
        _write_audit(bound_case.case_id, f"runs/{run_id}.input.json", {
            "run_id": run_id, "model_input": initial_input, "system_prompt": {"content": prompt.content, **metadata},
            "tools": tool_definitions(), "configuration": config.public_metadata()})
        while True:
            if used["finish"] >= MAX_FINISH_CALLS:
                return stopped("tool_budget_exhausted")
            if len(audit["model_calls"]) >= MAX_MODEL_CALLS:
                return stopped("model_budget_exhausted")
            targeted = {"propose_hypothesis_status": bool(formal.hypotheses),
                        "propose_evidence_gap_update": bool(formal.gaps),
                        "search_evidence_gaps": bool(formal.gaps),
                        "propose_evidence_link": bool(formal.hypotheses) or any(p.kind == "hypothesis" for p in proposals)}
            available = [d for d in tool_definitions() if d["function"]["name"] == "finish_investigation"
                         or (not investigation_closed() and
                             targeted.get(d["function"]["name"], True) and
                             (d["function"]["name"] not in actions.PROPOSAL_TOOLS or len(proposals) < 6))]
            if investigation_closed() and recovery is None:
                response = tools._failure(AppError(
                    "finish_required" if finish_required else "tool_budget_exhausted" if non_finish_exhausted() else "no_progress_limit",
                    "Investigation tools are unavailable; submit the answer with current material through finish_investigation"))
                _tool_recovery(response, "finish_investigation")
                recovery = response["error"]
            if investigation_closed():
                _finish_only_recovery(recovery)
            state = model_context(initial_input, knowledge, recovery)
            visible_units = frozenset(row["id"] for row in state["materials"]["info_units"]["items"])
            request_messages = [{"role": "system", "content": prompt.content},
                                {"role": "user", "content": json.dumps(state, ensure_ascii=False)}]
            choice = "required" if investigation_closed() or recovery and recovery.get("code") == "finish_required" else "auto"
            call_metadata = {**metadata, "information_acquisition": knowledge.acquisition(),
                             "available_tools": [d["function"]["name"] for d in available]}
            payload = llm_client.tool_request_payload(config, request_messages, available, tool_choice=choice)
            call_audit = {"step": len(audit["model_calls"]) + 1, **call_metadata, "request": deepcopy(payload)}
            audit["model_calls"].append(call_audit)
            persist()
            with trace.observation("Investigation Agent · model call", as_type="generation", input=payload,
                    model=config.model, version=prompt.version,
                    model_parameters={"max_completion_tokens": config.max_completion_tokens}, metadata=call_metadata) as generation:
                reply = await llm_client.call_tools(config, request_messages, available, tool_choice=choice)
                generation.record_reply(reply)
            call_audit.update(raw_response=reply.response, message=deepcopy(reply.message))
            calls = reply.message.get("tool_calls")
            if not calls:
                finish_required = True
                if recovery is None:
                    response = tools._failure(AppError("finish_required", "Deliver the answer through finish_investigation, with accurate answer_basis and unresolved conditions."))
                    _tool_recovery(response, "finish_investigation")
                    recovery = response["error"]
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
            permitted = category == "finish" or category in {"read", "proposal"} and not investigation_closed()
            if category == "finish" or category in {"read", "proposal"} and not non_finish_exhausted():
                used[category] += 1
            tool_audit = {"call_id": call["id"], "name": name, "raw_arguments": raw, "executed": False}
            audit["tool_calls"].append(tool_audit)
            args, finished, fingerprint = {}, None, None
            signature = (frozenset(seen["units"]), frozenset(seen["relations"]), frozenset(seen["evidence"]),
                tuple(p.proposal_id for p in proposals), json.dumps(knowledge.acquisition(), sort_keys=True))
            with trace.observation("Investigation Agent · tool call", as_type="tool", input={"name": name, "arguments": raw},
                    metadata={"tool_name": name if category else "unsupported_tool", "attempt": attempts}) as tool_span:
                try:
                    args = _arguments(name, raw)
                    fingerprint = _fingerprint(name, args, visible_info_unit_ids=visible_units)
                    tool_audit["arguments"] = deepcopy(args)
                    if name == "search_info_units":
                        tool_audit["excluded_info_unit_ids"] = sorted(visible_units)
                    if not permitted:
                        raise AppError("finish_required" if finish_required else "tool_budget_exhausted",
                                       "Investigation tools are closed; only finish_investigation may be called")
                    prior = fingerprints.get(fingerprint)
                    if prior and prior["successful"] and name in READ_TOOLS:
                        response = {**deepcopy(prior["result"]), "cached": True}
                    else:
                        if prior and (prior["successful"] or prior["state"] == signature):
                            raise AppError("duplicate_query", "Equivalent call was already attempted in this working state")
                        tool_audit["executed"] = True
                        executed += 1
                        if name in READ_TOOLS:
                            internal = {"exclude_info_unit_ids": visible_units} if name == "search_info_units" else {}
                            if name == "search_evidence_gaps":
                                internal = {"reasoning": formal}
                            response = getattr(tools, name)(bound_case, **args, **internal)
                            if response["status"] not in {"error", "invalid_request"}:
                                _remember(name, response, seen)
                        elif name in actions.PROPOSAL_TOOLS:
                            draft = list(proposals)
                            action_args = deepcopy(args)
                            if name == "propose_evidence_gap":
                                action_args.update(action_args.pop("hypothesis_targets"))
                            if name == "propose_evidence_gap_update":
                                _include_gap_targets(work_context, formal, {action_args["gap_id"]})
                            response = actions.PROPOSAL_TOOLS[name](bound_case, seen, work_context, draft, **action_args)
                            proposals = draft
                        else:
                            finished = actions.finish_investigation(bound_case, seen, work_context, goal, proposals,
                                **args, knowledge=knowledge,
                                read_budget_exhausted=investigation_closed(),
                                budget_exhausted=non_finish_exhausted())
                            result, answer = finished
                            open_questions, completion = result.open_questions, result.completion_status
                            answer_basis = args["answer_basis"]
                            response = {"status": "ok", "completion_status": completion}
                    fingerprints[fingerprint] = {"successful": response["status"] not in {"error", "invalid_request"},
                        "state": signature, "result": deepcopy(response)}
                except (AppError, ValidationError) as exc:
                    response = tools._failure(exc if isinstance(exc, AppError) else AppError("invalid_tool_arguments", "Fields violate their contract"))
                    response["error"]["validation_details"] = exc.details if isinstance(exc, AppError) else _validation_details(exc)
                    if fingerprint is not None and response["error"]["code"] != "duplicate_query":
                        fingerprints[fingerprint] = {"successful": False, "state": signature}
                except Exception as exc:
                    tool_audit.update(exception_type=type(exc).__name__, error_code="investigation_tool_failed")
                    response = tools._failure(AppError("investigation_tool_failed", "Tool failed; no artifact was accepted"), status="error")
                    if fingerprint is not None:
                        fingerprints[fingerprint] = {"successful": False, "state": signature}
                _tool_recovery(response, name, args, bound_case)
                failed = response["status"] in {"invalid_request", "error"}
                recovery = deepcopy(response["error"]) if failed else None
                if recovery:
                    recovery["tool"] = name
                    argument_text = json.dumps(args, ensure_ascii=False) if args else raw
                    recovery["arguments"] = deepcopy(args) if args and len(argument_text) <= 4000 else {
                        "excerpt": argument_text[:2000], "context_truncated": len(argument_text) > 2000}
                    details = recovery.get("validation_details", {})
                    errors = details.get("errors", [])
                    if len(errors) > 8:
                        details["errors"] = errors[:8]
                        details["omitted_error_count"] = len(errors) - 8
                        recovery["next_action"]["steps"] = recovery["next_action"]["steps"][:8]
                if response.get("has_more"):
                    limitations.append(f"{name}: truncated result; additional material may remain.")
                if name == "search_evidence_gaps" and not failed and response["items"]:
                    # Only discovered targets enter the existing validation and
                    # receipt context. Keep full revisions/associations here;
                    # the model receives only the tool's bounded projection.
                    gap_ids = {g["id"] for g in response["items"][0]["gaps"]}
                    _include_gap_targets(work_context, formal, gap_ids)
                merged_keys = knowledge.merge(name, args, response, executed=tool_audit["executed"])
                read_progress = name in READ_TOOLS and not failed and bool(
                    merged_keys & (_context_keys(model_context(initial_input, knowledge, recovery)) - _context_keys(state)))
                progressed = read_progress if name in READ_TOOLS else not failed and not response.get("cached") and not response.get("no_change")
                if category in {"read", "proposal"}:
                    no_progress = 0 if progressed else no_progress + 1
                tool_audit.update(read_progress=bool(read_progress), no_progress=no_progress)
                tool_audit["result"] = deepcopy(response)
                tool_audit["information_acquisition"] = knowledge.acquisition()
                tool_span.update(output=response, metadata={"information_acquisition": knowledge.acquisition(),
                    "read_progress": bool(read_progress), "no_progress": no_progress,
                    **({"excluded_info_unit_ids": sorted(visible_units)} if name == "search_info_units" else {})})
            persist()
            if finished:
                stop_reason = "finish_investigation"
                output = snapshot(answer, "completed")
                audit.update(status="success", stop_reason=stop_reason, result=output)
                # Save every validated artifact before the single workspace write.
                _write_audit(bound_case.case_id, output_path, audit)
                # The shared tool budget limits investigation, not a validated Finish's commit.
                if proposals:
                    try:
                        def validate_commit(current_case):
                            actions.artifacts(current_case, seen, work_context, goal, proposals, open_questions, completion)
                        workspace_commit = commit_investigation(case, issue_key, run_id, proposals,
                            audit["proposal_receipt"], validate=validate_commit)
                    except Exception as exc:
                        workspace_commit = {"status": "conflict" if isinstance(exc, AppError) and exc.status_code == 409 else "failed",
                            "changes": [], "error": {"code": getattr(exc, "code", "workspace_commit_failed"), "message": str(exc)}}
                        limitations.append("本轮提案未写入推理工作区；结果已保留，可检查后重新调查。")
                else:
                    workspace_commit = {"status": "no_change", "changes": []}
                output = snapshot(answer, "completed")
                audit["result"] = output
                try:
                    _write_audit(bound_case.case_id, output_path, audit)
                except AppError:
                    # The workspace's atomic run journal remains authoritative.
                    output["limitations"].append("最终审计输出保存失败；已写入变更以工作区运行记录为准。")
                record_final_state()
                trace.finish_success(output)
                return output
    except Exception as exc:
        error = trace.error_payload(exc)
        stop_reason = "model_failed" if isinstance(exc, llm_client.LLMFailure) else "error"
        persist()
        partial = isinstance(exc, (llm_client.LLMFailure, AppError)) and bool(proposals) and getattr(exc, "code", None) != "investigation_audit_failed"
        audit.update(status="partial" if partial else "failed", error=error, stop_reason=stop_reason,
                     result=snapshot(None, "failed"))
        if isinstance(exc, llm_client.LLMFailure):
            audit["provider_response"] = exc.response
        record_final_state()
        trace.finish_error(exc)
        _write_audit(bound_case.case_id, output_path, audit)
        if partial:
            return snapshot(None, "failed")
        if isinstance(exc, AppError):
            exc.details = {**exc.details, "run_id": run_id}
        raise
