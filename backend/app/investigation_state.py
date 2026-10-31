"""Run-local working state; domain records remain existing tool projections."""
from __future__ import annotations

from copy import deepcopy
import json
import re
from pydantic import ValidationError

from app.errors import AppError

MAX_CONTEXT_CHARS = 24000  # Deterministic character proxy, independent of provider tokenizer.
MAX_DIRECTORY_ITEMS = 80
MAX_DIRECTORY_CHARS = 6000
READ_TOOLS = frozenset({"search_info_units", "get_relations", "get_evidence"})


def case_info_directory(case):
    """Discovery metadata only; never grant content or SourceRef permissions."""
    items, size = [], 0
    for unit in sorted(case.info_units, key=lambda u: u.id):
        row = {"id": unit.id, "title": unit.title[:120], "type": unit.type.value,
               "title_truncated": len(unit.title) > 120}
        cost = len(json.dumps(row, ensure_ascii=False))
        if len(items) >= MAX_DIRECTORY_ITEMS or size + cost > MAX_DIRECTORY_CHARS:
            break
        items.append(row)
        size += cost
    total = len(case.info_units)
    return {"total_count": total, "items": items, "context_truncated": len(items) < total,
            "omitted_count": total - len(items)}


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
        self.initial_scope = deepcopy(initial.get("evidence_context", {}))
        for row in self.initial_scope.get("items", []):
            self.source_refs[self.ref_key(row)] = deepcopy(row)
        self.priority_ids = list(self.info_units)
        self.focus_ids = set()
        self.notices = []
        if self.initial_scope.get("has_more") or self.initial_scope.get("errors") or self.initial_scope.get("status") in {"error", "invalid_request"}:
            self.notices.append({"tool": "initial_sources", **{k:v for k,v in self.initial_scope.items() if k != "items"}})
        if any(v for k,v in canvas.items() if k.startswith("has_more")):
            self.notices.append({"tool": "initial_canvas", "message": "Initial workspace projection is partial; use case read tools for other material."})

    @staticmethod
    def ref_key(row):
        return f"{row['info_unit_id']}:{row['source_ref_index']}"

    def merge(self, name, args, response, *, executed=True):
        """Merge accepted projections and return the material keys actually returned."""
        material_keys = set()
        if response["status"] not in {"invalid_request", "error"}:
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
            self.focus_ids = set(args.get("info_unit_ids", touched)) if name in READ_TOOLS else self.focus_ids
        # Audit history stays on the server. Notices preserve distinct scopes,
        # including a truncated query after a later unrelated successful query.
        if name in READ_TOOLS:
            key = (name, json.dumps(args, sort_keys=True, ensure_ascii=False))
            self.notices = [n for n in self.notices if n.get("key") != key]
            if response.get("has_more") or response["status"] in {"empty", "error"}:
                self.notices.append({"key": key, "tool": name, "arguments": deepcopy(args),
                    "status": response["status"], "has_more": response.get("has_more", False),
                    "error": {k:v for k,v in (response.get("error") or {}).items() if k in {"code", "message"}}})
        self.activities.append({"tool": name, "arguments": deepcopy(args), "executed": executed,
            "cached": bool(response.get("cached")), "status": response["status"],
            "has_more": response.get("has_more", False), "error": deepcopy(response.get("error"))})
        return material_keys

    def dump(self):
        return deepcopy({"info_units": self.info_units, "relations": self.relations,
            "source_refs": self.source_refs, "work_context": self.work_context,
            "activities": self.activities, "proposals": self.proposals, "initial_scope": self.initial_scope})

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
            row.get("quote_excerpt", ""), row.get("source_ref", {}).get("quote", ""))))
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
              "gaps": ("id", "content", "hypothesis_ids", "info_unit_ids", "status", "content_truncated", "references_truncated"),
              "links": ("id", "hypothesis_id", "info_unit_id", "meaning", "rationale", "rationale_truncated")}
    reasoning = {kind: [{k:deepcopy(row[k]) for k in keys if k in row}
                        for row in workspace.get("reasoning", {}).get(kind, [])] for kind, keys in fields.items()}
    reasoning["has_more"] = workspace.get("reasoning", {}).get("has_more", False)
    result = {"goal": goal, "current_issue": workspace.get("current_issue"),
        "core_issue": initial.get("core_issue"),
        "case_info_directory": deepcopy(initial["case_info_directory"]),
        "materials": {
            "info_units": section(sorted(knowledge.info_units.items(), key=lambda kv:rank(kv[1])), 6500),
            "relations": section(sorted(knowledge.relations.items(), key=relation_rank), 3500),
            "source_refs": section(sorted(knowledge.source_refs.items(), key=lambda kv:rank(kv[1])), 7500)},
        "reasoning": deepcopy(reasoning),
        "proposals": deepcopy(knowledge.proposals),
        "notices": [{k:v for k,v in n.items() if k != "key"} for n in knowledge.notices[-6:]],
        "recovery": deepcopy(recovery)}
    if len(knowledge.notices) > 6:
        result["notices"].append({"context_truncated": True, "omitted_count": len(knowledge.notices) - 6})
    # Successful proposals and dependency IDs remain visible. Trim material
    # projections and directory, never the authoritative KnowledgeState.
    sections = list(result["materials"].values())
    while len(json.dumps(result, ensure_ascii=False)) > MAX_CONTEXT_CHARS and any(s["items"] for s in sections):
        target = max((s for s in sections if s["items"]), key=lambda s:len(json.dumps(s, ensure_ascii=False)))
        row = target["items"].pop()
        key = KnowledgeState.ref_key(row) if "source_ref_index" in row else row["id"]
        target["omitted_ids"].append(key); target["context_truncated"] = True
    directory = result["case_info_directory"]
    while len(json.dumps(result, ensure_ascii=False)) > MAX_CONTEXT_CHARS and directory["items"]:
        directory["items"].pop(); directory["context_truncated"] = True; directory["omitted_count"] += 1
    if len(json.dumps(result, ensure_ascii=False)) > MAX_CONTEXT_CHARS:
        raise AppError("investigation_context_limit", "Required task and proposal context exceeds the context budget")
    return result
