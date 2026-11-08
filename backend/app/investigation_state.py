"""Run-owned Workspace and bounded material/context projections."""
from __future__ import annotations

from copy import deepcopy
import json
import re
from pydantic import ValidationError

from app.errors import AppError

MAX_CONTEXT_CHARS = 24000  # Deterministic character proxy, independent of provider tokenizer.
READ_TOOLS = frozenset({"search_info_units", "get_relations", "get_evidence", "search_evidence_gaps"})


def tool_feedback(name, args, response, raw=""):
    """Recovery belonging to one execution, including successful no-change results."""
    failed = response["status"] in {"invalid_request", "error"}
    fields = ("code", "message", "no_change", "applied", "argument_loc", "validation_details",
              "recovery_hint", "next_action", "fix")
    if not failed and not any(response.get(key) for key in fields if key != "applied"):
        return None
    feedback = {key: deepcopy(response[key]) for key in fields if key in response}
    if failed:
        feedback.update(deepcopy(response.get("error") or {}))
    feedback.update(tool=name, status=response["status"], arguments=deepcopy(args) if args else {"raw": raw})
    return feedback


def previous_tool_result(name, call_id, raw, response, args):
    """One lossless execution envelope; never an accumulated conversation history."""
    try:
        arguments = json.loads(raw)
    except (ValueError, TypeError, RecursionError):
        arguments = {"raw": raw}
    failed = response["status"] in {"invalid_request", "error"}
    effect = ("failed" if failed else "no_change" if response.get("no_change") else
              "applied" if response.get("applied") else "read" if name in READ_TOOLS else "finished")
    notices = []
    if name in READ_TOOLS and (response.get("has_more") or response["status"] == "empty"):
        notices.append({key: deepcopy(response[key]) for key in
                        ("status", "has_more", "total_count", "next_offset") if key in response})
    return {"tool_name": name, "tool_call_id": call_id, "arguments": arguments, "raw_arguments": raw,
            "execution_status": "failed" if failed else "succeeded", "effect_status": effect,
            "result": deepcopy(response), "notices": notices,
            "recovery": tool_feedback(name, args, response, raw)}


class RunWorkspace:
    """The sole mutable reasoning source for one Investigation; never persisted by tools."""
    def __init__(self, current, issue, run_id):
        from app.reasoning_models import ReasoningState
        self.current = current.model_copy(deep=True)
        self.issue, self.run_id = issue, run_id
        self.baseline = self.reasoning.model_copy(deep=True) if issue is not None else ReasoningState()
        self.initial_visible_ids = set(self.current.workspaces[issue].canvas.visible_node_ids) if issue is not None else set()

    @property
    def reasoning(self):
        from app.reasoning_models import ReasoningState
        return self.current.workspaces[self.issue].reasoning if self.issue is not None else ReasoningState()

    def context(self):
        return {"current_issue": self.issue, "reasoning": self.reasoning.model_dump(mode="json")} if self.issue is not None else None


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
    def __init__(self, initial, workspace=None):
        self.workspace = workspace
        self.info_units, self.relations, self.source_refs = {}, {}, {}
        self.work_context = deepcopy(initial.get("work_context", {}))
        self.reasoning_ids = {row["id"] for kind in ("hypotheses", "gaps")
                              for row in self.work_context.get("reasoning", {}).get(kind, [])}
        self.work_context.pop("reasoning", None)
        self.activities, self.proposals = [], []
        canvas = self.work_context.get("canvas", {})
        for row in [*initial.get("selected_info_units", []), *canvas.get("info_units", [])]:
            self.info_units[row["id"]] = deepcopy(row)
        self.relations.update((r["id"], deepcopy(r)) for r in canvas.get("relations", []))
        self.priority_ids = list(self.info_units)
        self.priority_source_keys = set()
        self.focus_ids = set()
        self.previous_tool_result = None

    @staticmethod
    def ref_key(row):
        return f"{row['info_unit_id']}:{row['source_ref_index']}"

    def merge(self, name, args, response, *, executed=True):
        """Merge accepted projections and return the material keys actually returned."""
        material_keys = set()
        if response["status"] not in {"invalid_request", "error"}:
            if name == "search_evidence_gaps" and response.get("items"):
                # Remember discovery IDs only; object values always come from
                # the current Working Workspace, never a cached tool projection.
                self.reasoning_ids.update(row["id"] for kind in ("hypotheses", "gaps")
                                          for row in response["items"][0][kind])
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
        self.activities.append({"tool": name, "arguments": deepcopy(args), "executed": executed,
            "cached": bool(response.get("cached")), "status": response["status"],
            "has_more": response.get("has_more", False), "error": deepcopy(response.get("error"))})
        return material_keys

    def dump(self):
        return deepcopy({"info_units": self.info_units, "relations": self.relations,
            "source_refs": self.source_refs, "work_context": self.work_context,
            "activities": self.activities, "proposals": self.proposals,
            "previous_tool_result": self.previous_tool_result})

    def acquisition(self):
        reads = [a for a in self.activities if a["tool"] in READ_TOOLS and a["executed"]]
        obtained = bool(self.info_units or self.source_refs)
        successful = [a for a in reads if a["status"] in {"ok", "empty", "truncated"}]
        return {"obtained_info_unit_count": len(self.info_units), "obtained_source_count": len(self.source_refs),
            "read_attempt_count": len(reads), "successful_read_count": len(successful),
            "read_error_count": sum(a["status"] == "error" for a in reads),
            "has_material": obtained, "has_read_result": bool(successful),
            "read_failed": any(a["status"] == "error" for a in reads)}


def model_context(initial, knowledge):
    """Bound materials and project current run objects; Proposals are audit only."""
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
        kept, size = [], 0
        for key, row in rows:
            cost = len(json.dumps(row, ensure_ascii=False))
            if size + cost <= allowance:
                kept.append(deepcopy(row)); size += cost
        return {"items": kept}
    workspace = knowledge.work_context
    fields = {"hypotheses": ("id", "content", "judgment", "work_status"),
              "gaps": ("id", "content", "hypothesis_ids", "basis_info_unit_ids", "addressing_info_unit_ids", "status"),
              "links": ("id", "hypothesis_id", "info_unit_id", "meaning", "rationale")}
    source = {}
    if knowledge.workspace is not None:
        from app.issue_workspaces import project_reasoning
        current = knowledge.workspace.reasoning
        touched = [c["object_id"] for p in knowledge.proposals for c in p.get("changes", [])]
        order = list(dict.fromkeys(reversed(touched)))
        def object_rank(obj):
            return (obj.id in order, -order.index(obj.id) if obj.id in order else 0,
                    obj.id in knowledge.reasoning_ids, obj.updated_at, obj.id)
        chosen = []
        for objects in (current.hypotheses, current.gaps):
            chosen.extend(sorted((obj for obj in objects if obj.id in knowledge.reasoning_ids or obj.id in order),
                                 key=object_rank, reverse=True)[:3])
        visible_hypotheses = sum(h.id in knowledge.reasoning_ids or h.id in order for h in current.hypotheses)
        source = project_reasoning(current, chosen, object_rank, content_chars=600,
                                   hypothesis_limit=min(5, max(3, visible_hypotheses)))
    reasoning = {kind: [{k: deepcopy(row[k]) for k in keys if k in row}
                        for row in source.get(kind, [])] for kind, keys in fields.items()}
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
                       if key != "changes" and value is not None and value != []}
                      for proposal in knowledge.proposals],
        "previous_tool_result": deepcopy(knowledge.previous_tool_result)}
    # Trim material projections, never the authoritative KnowledgeState.
    sections = list(result["materials"].values())
    while len(json.dumps(result, ensure_ascii=False)) > MAX_CONTEXT_CHARS and any(s["items"] for s in sections):
        target = max((s for s in sections if s["items"]), key=lambda s:len(json.dumps(s, ensure_ascii=False)))
        target["items"].pop()
    if len(json.dumps(result, ensure_ascii=False)) > MAX_CONTEXT_CHARS:
        raise AppError("investigation_context_limit", "Required task, previous tool result and proposal context exceeds the context budget")
    return result
