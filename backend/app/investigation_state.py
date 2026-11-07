"""Run-local working state; domain records remain existing tool projections."""
from __future__ import annotations

from copy import deepcopy
import json
import re
from pydantic import ValidationError

from app.errors import AppError

MAX_CONTEXT_CHARS = 24000  # Deterministic character proxy, independent of provider tokenizer.
MAX_FEEDBACK_CHARS = 8000
READ_TOOLS = frozenset({"search_info_units", "get_relations", "get_evidence", "search_evidence_gaps"})


def tool_feedback(name, args, response, raw=""):
    """One-turn actionable feedback; successful material stays in KnowledgeState."""
    failed = response["status"] in {"invalid_request", "error"}
    fields = ("code", "message", "no_change", "pending", "argument_loc", "validation_details",
              "recovery_hint", "next_action", "fix")
    if not failed and not any(response.get(key) for key in fields if key != "pending"):
        return None
    feedback = {key: deepcopy(response[key]) for key in fields if key in response}
    if failed:
        feedback.update(deepcopy(response.get("error") or {}))
    feedback.update(tool=name, status=response["status"], arguments=deepcopy(args) if args else {"raw": raw})
    return feedback


def _argument_view(arguments):
    text = json.dumps(arguments, ensure_ascii=False)
    return deepcopy(arguments) if len(text) <= 1000 else {
        "excerpt": text[:600], "context_truncated": True, "omitted_chars": len(text) - 600}


def feedback_view(feedback):
    """Bound the latest feedback before trimming material; leave audits lossless."""
    if feedback is None:
        return None
    original = deepcopy(feedback)
    details = original.get("validation_details", {})
    errors = details.get("errors", [])
    total = len(errors) + details.get("omitted_error_count", 0)
    # Preserve error fields and actions, rather than spending space on a large
    # argument dump or repeated independent errors. Head + tail retains both
    # the cause and a trailing repair instruction in unusually long messages.
    def compact(value, chars, path, omitted):
        if isinstance(value, str) and len(value) > chars:
            omitted.append(path)
            half = chars // 2
            return value[:half] + f" ... [{len(value) - 2 * half} chars omitted] ... " + value[-half:]
        if isinstance(value, dict):
            return {key: compact(item, chars, f"{path}.{key}", omitted) for key, item in value.items()}
        if isinstance(value, list):
            if len(value) > 8:
                omitted.append(path)
            return [compact(item, chars, f"{path}.{index}", omitted) for index, item in enumerate(value[:8])]
        return value

    for count in range(min(len(errors), 8), -1, -1):
        if errors and count == 0:
            break
        result = deepcopy(original)
        if errors:
            result["validation_details"]["errors"] = errors[:count]
            if total > count:
                result["validation_details"]["omitted_error_count"] = total - count
            if "steps" in result.get("next_action", {}):
                result["next_action"]["steps"] = result["next_action"]["steps"][:count]
        arguments = result.get("arguments")
        if arguments is not None:
            result["arguments"] = _argument_view(arguments)
        for chars in (1000, 400, 160):
            omitted = []
            candidate = compact(result, chars, "recovery", omitted)
            if omitted or total > count or candidate.get("arguments", {}).get("context_truncated"):
                candidate["context_truncated"] = True
                candidate["truncated_fields"] = omitted[:8]
                if len(omitted) > 8:
                    candidate["omitted_truncated_field_count"] = len(omitted) - 8
            if len(json.dumps(candidate, ensure_ascii=False)) <= MAX_FEEDBACK_CHARS:
                return candidate
    # Server feedback has a fixed shallow shape. Drop unrelated exception
    # diagnostics before considering an unrenderable required-context failure.
    minimal = {key: value for key, value in original.items() if key in {
        "tool", "status", "code", "message", "no_change", "pending", "argument_loc", "fix",
        "recovery_hint", "constraints"}}
    minimal["context_truncated"] = True
    minimal["arguments"] = _argument_view(original.get("arguments", {}))
    action = original.get("next_action", {})
    minimal["next_action"] = {"retry_tool": action.get("retry_tool"), "steps": [
        {key: value for key, value in step.items() if key in {"tool", "code", "loc", "instruction"}}
        for step in action.get("steps", [])[:1]]}
    if errors:
        minimal["validation_details"] = {"errors": [{key: value for key, value in errors[0].items()
            if key in {"code", "loc", "argument_loc", "message", "fix"}}], "omitted_error_count": total - 1}
    omitted = []
    result = compact(minimal, 160, "recovery", omitted)
    result["truncated_fields"] = omitted[:8]
    if len(omitted) > 8:
        result["omitted_truncated_field_count"] = len(omitted) - 8
    if len(json.dumps(result, ensure_ascii=False)) > MAX_FEEDBACK_CHARS:
        raise AppError("investigation_context_limit", "Required actionable tool feedback exceeds the context budget")
    return result


def validation_issues(exc, prefix=(), fix="Correct the indicated field using the tool schema"):
    """Keep independent diagnostics in the existing error envelope."""
    if isinstance(exc, ValidationError):
        rows = [{"loc": list(e["loc"]), "code": e["type"], "message": e["msg"], "category": "structure"}
                for e in exc.errors(include_input=False, include_context=False, include_url=False)]
    else:
        rows = exc.details.get("errors") or [{"loc": [], "code": exc.code, "message": str(exc)}]
    category = "structure" if isinstance(exc, ValidationError) else exc.details.get("category", "reference")
    return [{**row, "loc": [*prefix, *row.get("loc", [])], "category": row.get("category", category),
             "message": row.get("message", str(exc)), "fix": row.get("fix", fix)} for row in rows]


class KnowledgeState:
    """Lossless run-local material store; model views are bounded projections."""
    def __init__(self, initial):
        self.info_units, self.relations, self.source_refs = {}, {}, {}
        self.work_context = deepcopy(initial.get("work_context", {}))
        self.activities, self.proposals = [], []
        canvas = self.work_context.get("canvas", {})
        for row in [*initial.get("selected_info_units", []), *canvas.get("info_units", [])]:
            self.info_units[row["id"]] = deepcopy(row)
        self.relations.update((r["id"], deepcopy(r)) for r in canvas.get("relations", []))
        self.priority_ids = list(self.info_units)
        self.priority_source_keys = set()
        self.focus_ids = set()
        self.notices = []
        if any(v for k,v in canvas.items() if k.startswith("has_more")):
            self.notices.append({"tool": "initial_canvas", "message": "Initial workspace projection is partial; use case read tools for other material."})

    @staticmethod
    def ref_key(row):
        return f"{row['info_unit_id']}:{row['source_ref_index']}"

    def merge(self, name, args, response, *, executed=True):
        """Merge accepted projections and return the material keys actually returned."""
        material_keys = set()
        if response["status"] not in {"invalid_request", "error"}:
            if name == "search_evidence_gaps" and response.get("items"):
                # Replace the bounded reasoning view, not the formal workspace
                # or the independent material/source store.
                self.work_context["reasoning"] = deepcopy(response["items"][0])
                material_keys.update((kind, row["id"]) for kind in ("hypotheses", "gaps", "links")
                                     for row in response["items"][0][kind])
            touched = []
            for row in response.get("items", []):
                if name == "search_info_units":
                    self.info_units[row["id"]] = deepcopy(row)
                    material_keys.add(("info_units", row["id"]))
                    touched.append(row["id"])
                elif name == "get_relations":
                    relation = row["relation"]
                    self.relations[relation["id"]] = deepcopy(relation)
                    material_keys.add(("relations", relation["id"]))
                    for unit in row["adjacent_info_units"]:
                        self.info_units[unit["id"]] = deepcopy(unit)
                        material_keys.add(("info_units", unit["id"]))
                        touched.append(unit["id"])
                elif name == "get_evidence":
                    self.source_refs[self.ref_key(row)] = deepcopy(row)
                    material_keys.add(("source_refs", self.ref_key(row)))
                    touched.append(row["info_unit_id"])
            self.priority_ids = list(dict.fromkeys([*touched, *self.priority_ids]))
            if name == "get_evidence":
                self.priority_source_keys = {self.ref_key(row) for row in response.get("items", [])}
            self.focus_ids = ({args["info_unit_id"]} if name == "get_evidence" else set(args.get("info_unit_ids", touched))
                              if name in READ_TOOLS - {"search_evidence_gaps"} else self.focus_ids)
        # Audit history stays on the server. Notices preserve distinct scopes,
        # including a truncated query after a later unrelated successful query.
        if name in READ_TOOLS:
            key = (name, json.dumps(args, sort_keys=True, ensure_ascii=False))
            if response["status"] not in {"invalid_request", "error"}:
                self.notices = [n for n in self.notices if n.get("key") != key and not (
                    name == "get_evidence" and n["tool"] == name
                    and n["arguments"].get("info_unit_id") == args["info_unit_id"]
                    and n.get("next_offset") == args.get("offset", 0))]
            if response["status"] not in {"invalid_request", "error"} and (
                    response.get("has_more") or response["status"] in {"empty", "truncated"}):
                self.notices.append({"key": key, "tool": name, "arguments": deepcopy(args),
                    "status": response["status"], "has_more": response.get("has_more", False),
                    **({k: response[k] for k in ("total_count", "next_offset") if k in response} if name == "get_evidence" else {}),
                    "error": {k:v for k,v in (response.get("error") or {}).items() if k in {"code", "message"}}})
        self.activities.append({"tool": name, "arguments": deepcopy(args), "executed": executed,
            "cached": bool(response.get("cached")), "status": response["status"],
            "has_more": response.get("has_more", False), "error": deepcopy(response.get("error"))})
        return material_keys

    def dump(self):
        return deepcopy({"info_units": self.info_units, "relations": self.relations,
            "source_refs": self.source_refs, "work_context": self.work_context,
            "activities": self.activities, "proposals": self.proposals})

    def acquisition(self):
        reads = [a for a in self.activities if a["tool"] in READ_TOOLS and a["executed"]]
        obtained = bool(self.info_units or self.source_refs)
        successful = [a for a in reads if a["status"] in {"ok", "empty", "truncated"}]
        return {"obtained_info_unit_count": len(self.info_units), "obtained_source_count": len(self.source_refs),
            "read_attempt_count": len(reads), "successful_read_count": len(successful),
            "read_error_count": sum(a["status"] == "error" for a in reads),
            "has_material": obtained, "has_read_result": bool(successful),
            "read_failed": any(a["status"] == "error" for a in reads)}


def model_context(initial, knowledge, recovery=None):
    """Send each material once, with explicit omissions and no activity history."""
    from app.investigation_tools import _normalized
    goal = initial["goal"]
    words = set(re.findall(r"[^\W_]+", _normalized(goal)))
    for part in re.findall(r"[\u3400-\u9fff]+", goal):
        words.update(part[i:i+2] for i in range(len(part)-1))
    def rank(row):
        uid = row.get("info_unit_id", row.get("id", ""))
        unit = knowledge.info_units.get(uid, {})
        text = _normalized(" ".join(str(value) for value in (
            row.get("title", ""), row.get("content", ""), unit.get("title", ""), unit.get("content", ""),
            row.get("source_ref", {}).get("quote", ""))))
        return (uid not in knowledge.focus_ids, -sum(w in text for w in words),
                knowledge.priority_ids.index(uid) if uid in knowledge.priority_ids else 100000, str(uid))
    def relation_rank(row):
        return min(rank({"id": uid}) for uid in (row[1]["source_id"], row[1]["target_id"]))
    def section(rows, allowance):
        kept, omitted, size = [], [], 0
        for key, row in rows:
            cost = len(json.dumps(row, ensure_ascii=False))
            if size + cost <= allowance:
                kept.append(deepcopy(row)); size += cost
            else:
                omitted.append(key)
        return {"items": kept, "context_truncated": bool(omitted), "omitted_ids": omitted}
    workspace = knowledge.work_context
    fields = {"hypotheses": ("id", "content", "judgment", "work_status", "content_truncated"),
              "gaps": ("id", "content", "hypothesis_ids", "basis_info_unit_ids", "addressing_info_unit_ids",
                       "basis_info_unit_ids_truncated", "addressing_info_unit_ids_truncated",
                       "status", "content_truncated", "references_truncated"),
              "links": ("id", "hypothesis_id", "info_unit_id", "meaning", "rationale", "rationale_truncated")}
    reasoning = {kind: [{k:deepcopy(row[k]) for k in keys if k in row}
                        for row in workspace.get("reasoning", {}).get(kind, [])] for kind, keys in fields.items()}
    reasoning["has_more"] = workspace.get("reasoning", {}).get("has_more", False)
    result = {"goal": goal, "current_issue": workspace.get("current_issue"),
        "materials": {
            "info_units": section(sorted(knowledge.info_units.items(), key=lambda kv:rank(kv[1])), 6500),
            "relations": section(sorted(knowledge.relations.items(), key=relation_rank), 3500),
            "source_refs": section(sorted(knowledge.source_refs.items(),
                key=lambda kv:(kv[0] not in knowledge.priority_source_keys, *rank(kv[1]))), 12000)},
        "reasoning": deepcopy(reasoning),
        # Transport defaults stay in the audit/adoption contract. Preserve all
        # populated fields and dependency IDs in the model's working memory.
        "proposals": [{key: deepcopy(value) for key, value in proposal.items()
                       if value is not None and value != []}
                      for proposal in knowledge.proposals],
        "notices": [{k:(_argument_view(v) if k == "arguments" else deepcopy(v))
                     for k,v in n.items() if k != "key"} for n in knowledge.notices[-6:]],
        "recovery": feedback_view(recovery)}
    if len(knowledge.notices) > 6:
        result["notices"].append({"context_truncated": True, "omitted_count": len(knowledge.notices) - 6})
    # Trim material projections, never the authoritative KnowledgeState.
    sections = list(result["materials"].values())
    while len(json.dumps(result, ensure_ascii=False)) > MAX_CONTEXT_CHARS and any(s["items"] for s in sections):
        target = max((s for s in sections if s["items"]), key=lambda s:len(json.dumps(s, ensure_ascii=False)))
        row = target["items"].pop()
        key = KnowledgeState.ref_key(row) if "source_ref_index" in row else row["id"]
        target["omitted_ids"].append(key); target["context_truncated"] = True
    if len(json.dumps(result, ensure_ascii=False)) > MAX_CONTEXT_CHARS:
        raise AppError("investigation_context_limit", "Required task and proposal context exceeds the context budget")
    return result
