"""Run-local working state; domain records remain existing tool projections."""
from __future__ import annotations

from copy import deepcopy
import json
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.errors import AppError

Text = Annotated[str, Field(min_length=1, max_length=600, pattern=r"\S")]
Identifier = Annotated[str, Field(min_length=1, max_length=80, pattern=r"\S")]
MAX_QUESTIONS = 20
MAX_CONTEXT_CHARS = 48000  # Deterministic character proxy, independent of provider tokenizer.


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


class StateModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class EvidencePointer(StateModel):
    info_unit_id: Text
    source_ref_index: Annotated[int, Field(ge=0)]


class InvestigationStepUpdate(StateModel):
    """One incremental upsert; execution indices are never model input."""
    id: Identifier
    question_id: Identifier
    objective: Text
    status: Literal["pending", "in_progress", "completed", "deferred"]
    result_note: Text | None

    @model_validator(mode="after")
    def terminal_note(self):
        if self.status in {"completed", "deferred"} and self.result_note is None:
            raise ValueError("Completed steps need a result; deferred steps need a specific reason")
        return self


class InvestigationStep(InvestigationStepUpdate):
    activity_indices: list[Annotated[int, Field(ge=0)]] = Field(default_factory=list)


class Question(StateModel):
    id: Identifier
    question: Annotated[str, Field(min_length=1, max_length=2000, pattern=r"\S")]
    status: Literal["pending", "in_progress", "answered", "blocked"]
    related_info_unit_ids: Annotated[list[Text], Field(max_length=80)]
    evidence_refs: Annotated[list[EvidencePointer], Field(max_length=64)]
    finding: Text | None
    block_reason: Text | None
    block_kind: Literal["evidence_missing", "tool_failure", "unsupported", "budget_exhausted"] | None

    @model_validator(mode="after")
    def consistent_status(self):
        if self.status == "blocked":
            if not self.block_reason or not self.block_kind:
                raise ValueError("Blocked questions require a specific block_reason and block_kind")
        elif self.block_reason is not None or self.block_kind is not None:
            raise ValueError("Only blocked questions may have blocking fields")
        if self.status == "answered" and (not self.finding or not (self.related_info_unit_ids or self.evidence_refs)):
            raise ValueError("Answered questions require a finding and obtained materials")
        return self


class QuestionState:
    def __init__(self, goal):
        self.items = {"Q1": Question(id="Q1", question=goal, status="pending", related_info_unit_ids=[],
            evidence_refs=[], finding=None, block_reason=None, block_kind=None)}
        self.activities = {"Q1": []}
        self.steps: dict[str, InvestigationStep] = {}

    def dump(self):
        return {"questions": [q.model_dump(mode="json") for q in self.items.values()],
                "activities": deepcopy(self.activities), "current_question_ids": self.resolve(None),
                "investigation_steps": [s.model_dump(mode="json") for s in self.steps.values()]}

    def resolve(self, ids):
        if ids is None:
            ids = [q.id for q in self.items.values() if q.status == "in_progress"]
            ids = ids or [q.id for q in self.items.values() if q.status == "pending"][:1] or ["Q1"]
        if not isinstance(ids, list) or not ids or len(ids) > MAX_QUESTIONS or any(not isinstance(i, str) for i in ids):
            raise AppError("invalid_question_reference", "Supply a nonempty list of current Question IDs")
        if len(set(ids)) != len(ids) or not set(ids) <= self.items.keys():
            raise AppError("invalid_question_reference", "Question IDs must be unique and already registered")
        return list(ids)

    def review_updates(self, updates, case, seen, context):
        """Build a diagnostic draft without accepting any invalid update batch."""
        from app.investigation_actions import artifacts
        draft, errors, parsed = deepcopy(self), [], []
        if not isinstance(updates, list) or len(updates) > MAX_QUESTIONS:
            return draft, [{"loc": ["questions"], "code": "invalid_questions",
                "message": "Submit a list of at most twenty Questions", "fix": "Use [] to reuse current validated Questions"}]
        for index, row in enumerate(updates):
            try:
                parsed.append(Question.model_validate(row, strict=True))
            except ValidationError as exc:
                errors.extend(validation_issues(exc, ["questions", index]))
        if len({q.id for q in parsed}) != len(parsed):
            errors.append({"loc": ["questions"], "code": "invalid_questions",
                "message": "Duplicate Question IDs", "fix": "Submit each Question ID once"})
        def error(loc, code, message, fix):
            errors.append({"loc": loc, "code": code, "message": message, "fix": fix})

        for q in parsed:
            loc = ["questions", q.id]
            if q.id == "Q1" and q.question != self.items["Q1"].question:
                error([*loc, "question"], "goal_scope_changed", "Q1 must retain the complete original goal",
                      "Restore Q1.question from question_state; add Questions for separate aspects")
            if q.related_info_unit_ids or q.evidence_refs:
                try:
                    artifacts(case, seen, context, "Question basis", [], [{"statement": q.finding or q.question[:600],
                        "info_unit_ids": q.related_info_unit_ids, "relation_ids": [],
                        "evidence_refs": [r.model_dump() for r in q.evidence_refs]}], [], "evidence_insufficient")
                except (AppError, ValidationError) as exc:
                    for issue in validation_issues(exc, fix="Use obtained current-case material and verified SourceRef indices"):
                        path = issue["loc"]
                        issue["loc"] = [*loc, *(path[2:] if path[:1] == ["findings"] else path)]
                        if issue["loc"][2:3] == ["relevant_info_unit_ids"]:
                            issue["loc"][2] = "related_info_unit_ids"
                        errors.append(issue)
            draft.items[q.id] = q
        if len(draft.items) > MAX_QUESTIONS:
            error(["questions"], "question_limit", "At most twenty run-local Questions", "Keep the current Question limit")
        for q in parsed:
            draft.activities.setdefault(q.id, [])
        return draft, errors

    def update(self, updates, case, seen, context):
        if not updates:
            raise AppError("invalid_questions", "Submit one to twenty unique Questions per batch")
        draft, errors = self.review_updates(updates, case, seen, context)
        if errors:
            raise AppError(errors[0]["code"], "Question updates were not accepted; fix all listed fields",
                           details={"category": "questions", "errors": errors})
        self.items, self.activities = draft.items, draft.activities
        return {"status": "ok", "updated_question_ids": [q["id"] for q in updates]}

    def open_questions(self):
        return [(q.question + ("：" + q.block_reason if q.block_reason else ""))[:600]
                for q in self.items.values() if q.status != "answered"]

    def record(self, activity_index, ids):
        for qid in ids:
            self.activities[qid].append(activity_index)

    def update_steps(self, updates, knowledge):
        """Upsert only supplied IDs atomically; omitted steps always survive."""
        from pydantic import TypeAdapter
        parsed = TypeAdapter(Annotated[list[InvestigationStepUpdate], Field(min_length=1, max_length=20)]).validate_python(updates, strict=True)
        draft, errors, ids = deepcopy(self.steps), [], set()

        def error(index, field, code, message, fix):
            errors.append({"loc": ["steps", index, field], "code": code, "message": message, "fix": fix})

        for index, row in enumerate(parsed):
            old = self.steps.get(row.id)
            if row.id in ids:
                error(index, "id", "duplicate_step", "Each step ID may appear only once per update", "Send each changed step once")
            ids.add(row.id)
            if row.question_id not in self.items:
                error(index, "question_id", "invalid_question_reference", "Step needs a registered Question", "Create the Question first")
                continue
            if old and row.question_id != old.question_id:
                error(index, "question_id", "step_scope_changed", "Registered step ownership cannot change", "Keep the original question_id; add a new step for other work")
            if old and old.status in {"completed", "deferred"}:
                if row.status not in {old.status, "in_progress"}:
                    error(index, "status", "invalid_step_transition", "Terminal steps may only retain status or reopen in_progress", "Reopen the same objective before continuing")
                if row.objective != old.objective:
                    error(index, "objective", "step_history_changed", "Terminal objective cannot be overwritten", "Reopen with the original objective first, or add a new step")
            if old and old.status == "in_progress" and row.status == "pending":
                error(index, "status", "invalid_step_transition", "Started work cannot return to pending", "Continue it or record its result/deferral")
            indices = list(old.activity_indices) if old else []
            if row.status == "completed":
                # An empty successful search is a real investigation result.
                # A Proposal, an invented index or prose alone is not execution.
                read_result = any(knowledge.activities[i]["tool"] in {"search_info_units", "get_relations", "get_evidence"}
                    and knowledge.activities[i]["status"] in {"ok", "empty", "truncated"} for i in indices)
                question = self.items[row.question_id]
                provided_basis = any(uid in knowledge.info_units or any(r["info_unit_id"] == uid for r in knowledge.source_refs.values())
                                     for uid in question.related_info_unit_ids)
                if not read_result and not provided_basis:
                    error(index, "status", "step_completion_without_basis", "No linked read result or obtained Question material supports handling this step",
                          "Investigate with step_id, use obtained Question material, or defer with a reason; Proposal submission alone does not complete investigation")
            draft[row.id] = InvestigationStep(**row.model_dump(), activity_indices=indices)
        if len(draft) > 20:
            errors.append({"loc": ["steps"], "code": "step_limit", "message": "At most twenty registered investigation steps",
                           "fix": "Update existing steps; registered history cannot be deleted"})
        if errors:
            raise AppError(errors[0]["code"], "Step updates were not accepted; fix all listed fields",
                           details={"category": "steps", "errors": errors})
        self.steps = draft
        return {"status": "ok", "updated_step_ids": [s.id for s in parsed]}

    def resolve_step(self, step_id, question_ids):
        if step_id is None:
            return None
        if not isinstance(step_id, str) or step_id not in self.steps:
            raise AppError("invalid_step_reference", "step_id must be registered in this run")
        step = self.steps[step_id]
        if question_ids != [step.question_id]:
            raise AppError("step_question_mismatch", "A step-associated read must target only its owning Question")
        if step.status in {"completed", "deferred"}:
            raise AppError("step_not_active", "Reopen a terminal step in_progress before another associated read")
        return step

    def record_step(self, step, activity_index):
        if step is not None:
            step.activity_indices.append(activity_index)
            if step.status == "pending":
                step.status = "in_progress"


class KnowledgeState:
    def __init__(self, initial):
        self.info_units, self.relations, self.source_refs = {}, {}, {}
        self.work_context = deepcopy(initial.get("work_context", {}))
        self.activities, self.findings, self.proposals = [], [], []
        for row in initial["selected_info_units"] + initial.get("work_context", {}).get("canvas", {}).get("info_units", []):
            self.info_units[row["id"]] = deepcopy(row)
        canvas = initial.get("work_context", {}).get("canvas", {})
        self.relations.update((r["id"], deepcopy(r)) for r in canvas.get("relations", []))
        self.initial_scope = deepcopy({"evidence_context": initial["evidence_context"],
            "canvas_omissions": {k: v for k, v in canvas.items() if k.startswith("has_more")}})
        for row in initial["evidence_context"]["items"]:
            self.source_refs[self.ref_key(row)] = deepcopy(row)

    @staticmethod
    def ref_key(row):
        return f"{row['info_unit_id']}:{row['source_ref_index']}"

    def merge(self, name, args, response, question_ids, *, step_id=None, executed=True):
        """Only successful read projections grant knowledge; failures remain explicit."""
        if response["status"] not in {"invalid_request", "error"}:
            for row in response.get("items", []):
                if name == "search_info_units":
                    self.info_units[row["id"]] = deepcopy(row)
                elif name == "get_relations":
                    relation = row["relation"]
                    self.relations[relation["id"]] = deepcopy(relation)
                    for unit in row["adjacent_info_units"]:
                        self.info_units[unit["id"]] = deepcopy(unit)
                elif name == "get_evidence":
                    self.source_refs[self.ref_key(row)] = deepcopy(row)
        activity = {"activity_index": len(self.activities), "tool": name, "arguments": deepcopy(args),
            "question_ids": list(question_ids), "step_id": step_id, "executed": executed,
            "cached": bool(response.get("cached")),
            "status": response["status"], "has_more": response.get("has_more", False),
            "error": deepcopy(response.get("error")),
            "obtained": [row.get("id") or row.get("relation", {}).get("id") or self.ref_key(row)
                         for row in response.get("items", [])]}
        if response.get("proposal_id"):
            activity["proposal_id"] = response["proposal_id"]
        self.activities.append(activity)
        return len(self.activities) - 1

    def dump(self):
        return deepcopy({"info_units": self.info_units, "relations": self.relations,
            "source_refs": self.source_refs, "initial_scope": self.initial_scope,
            "work_context": self.work_context,
            "activities": self.activities, "findings": self.findings, "proposals": self.proposals})

    def available_work(self, case, questions):
        """Discovery hints, NOT obtained evidence or a semantic relevance judgment.

        Derive from current records/activity every time; no second task/lead store.
        A short list may be investigated or explicitly declined, never exhausted
        as a completion quota. Looking at this index grants no citation permission.
        """
        from app import investigation_tools as tools

        units = {u.id: u for u in case.info_units}
        result = {}
        for q in questions.items.values():
            anchors = set(q.related_info_unit_ids)
            if q.id == "Q1":
                anchors.update(self.info_units)
                anchors.update(row["info_unit_id"] for row in self.source_refs.values())
            else:
                for activity in self.activities:
                    if q.id in activity["question_ids"] and activity["status"] not in {"error", "invalid_request"}:
                        anchors.update(uid for uid in activity["arguments"].get("info_unit_ids", []) if uid in units)
                        anchors.update(uid for uid in activity["obtained"] if uid in units)
            neighbors = {uid for r in case.relations if {r.source_id, r.target_id} & anchors
                         for uid in [r.source_id, r.target_id, *r.evidence_unit_ids]} - anchors
            # Generic language matching, not case-specific terms or a truth test.
            text = " ".join([q.question, q.finding or "", q.block_reason or ""])
            terms = set(re.findall(r"[^\W_]+", tools._normalized(text)))
            for part in re.findall(r"[\u3400-\u9fff]+", text):
                terms.update(part[i:i+2] for i in range(len(part)-1))
            candidates = []
            for uid, unit in units.items():
                score, weighted = tools._keyword_score(unit, list(terms))
                if uid in anchors:
                    missing = [i for i in range(len(unit.source_refs)) if self.ref_key({"info_unit_id": uid, "source_ref_index": i}) not in self.source_refs]
                    partial = any(row.get("quote_truncated") for row in self.source_refs.values() if row["info_unit_id"] == uid)
                    if missing or partial:
                        candidates.append((3, score, {"lead_id": "source:" + uid, "kind": "unread_source",
                            "info_unit_id": uid, "title": unit.title[:120],
                            "reason": "Associated material has unprovided sources or only an excerpt",
                            "next_tool": "get_evidence", "arguments": {"info_unit_ids": [uid]}}))
                elif uid not in self.info_units and not any(r["info_unit_id"] == uid for r in self.source_refs.values()) and (uid in neighbors or weighted >= 3):
                    candidates.append((2 if uid in neighbors else 1, score, {
                        "lead_id": "unit:" + uid, "kind": "unread_info_unit", "info_unit_id": uid,
                        "title": unit.title[:120], "reason": "One-hop relation" if uid in neighbors else "Text overlap with this Question/missing condition",
                        "next_tool": "get_evidence" if unit.source_refs else "search_info_units",
                        "arguments": {"info_unit_ids": [uid]} if unit.source_refs else {"query": unit.title[:tools.MAX_QUERY_CHARS]}}))
            for index, activity in enumerate(self.activities):
                if q.id not in activity["question_ids"] or not activity["has_more"] or activity["tool"] not in {"search_info_units", "get_relations"}:
                    continue
                # A wider successful query supersedes a bounded earlier prefix.
                args = activity["arguments"]
                if any(a["tool"] == activity["tool"] and a["status"] in {"ok", "empty", "truncated"}
                       and {k:v for k,v in a["arguments"].items() if k != "limit"} == {k:v for k,v in args.items() if k != "limit"}
                       and a["arguments"].get("limit", tools.DEFAULT_LIMIT) > args.get("limit", tools.DEFAULT_LIMIT)
                       for a in self.activities[index+1:]):
                    continue
                candidates.append((3, 0, {"lead_id": f"query:{index}", "kind": "truncated_query",
                    "reason": "Returned prefix does not exhaust this query; widen/refine if useful",
                    "next_tool": activity["tool"], "arguments": {**args, "limit": tools.MAX_LIMIT}}))
            candidates.sort(key=lambda row: (-row[0], -row[1], row[2]["lead_id"]))
            result[q.id] = {"question_id": q.id, "items": [row[2] for row in candidates[:tools.MAX_LIMIT]],
                "has_more": len(candidates) > tools.MAX_LIMIT,
                "scope": "Heuristic discovery index only. Inspect valuable leads, or explain specifically why they cannot improve this Question. Not proof of missing evidence."}
        return result


def _preview(value, text_limit=500):
    if isinstance(value, str) and len(value) > text_limit:
        return {"excerpt": value[:text_limit], "context_truncated": True,
                "full_characters": len(value), "recovery": "Original in run audit; use read tools for full source wording"}
    if isinstance(value, list):
        return [_preview(v, text_limit) for v in value]
    if isinstance(value, dict):
        return {k: _preview(v, text_limit) for k, v in value.items()}
    return value


def model_context(initial, knowledge, questions, budget, recovery):
    """Bound each section and disclose omitted records; no summarization model."""
    # Work context is already bounded by the Issue service, retained as user data.
    result = {**deepcopy(initial), "question_state": {}, "knowledge_state": {},
              "remaining_budget": budget, "recovery": recovery}
    result.pop("evidence_context", None)  # Now held in KnowledgeState, not duplicated each turn.
    result.pop("selected_info_units", None)
    current = questions.resolve(None)
    preferred = {uid for a in knowledge.activities if set(a["question_ids"]) & set(current)
                 for uid in a["arguments"].get("info_unit_ids", [])}
    preferred.update(uid for qid in current for uid in questions.items[qid].related_info_unit_ids)

    def section(rows, allowance):
        kept, omitted = [], []
        size = 0
        for key, row in rows:
            preview = _preview(row)
            cost = len(json.dumps(preview, ensure_ascii=False))
            if size + cost <= allowance:
                kept.append(preview)
                size += cost
            else:
                omitted.append(key)
        return {"items": kept, "context_truncated": bool(omitted), "omitted_ids": omitted}

    qdump = questions.dump()
    # Always expose the status of every task, even when its detailed plan exceeds the view budget.
    result["question_state"] = {"current_question_ids": current,
        "progress": [{"id": q.id, "question": q.question[:200], "status": q.status,
            "question_truncated": len(q.question) > 200,
            "block_kind": q.block_kind, "block_reason": _preview(q.block_reason, 240)}
            for q in questions.items.values()],
        "details": section(sorted([(q["id"], q) for q in qdump["questions"]], key=lambda row: row[0] not in current), 12000)}
    result["investigation_steps"] = {
        "progress": [{"id": s.id, "question_id": s.question_id, "status": s.status,
                      "objective": s.objective[:200], "objective_truncated": len(s.objective) > 200,
                      "activity_indices": list(s.activity_indices)} for s in questions.steps.values()],
        "details": section([(s["id"], s) for s in qdump["investigation_steps"]], 8000)}
    result["knowledge_state"] = {
        "info_units": section(sorted(knowledge.info_units.items(), key=lambda r: r[0] not in preferred), 6000),
        "relations": section(sorted(knowledge.relations.items(), key=lambda row:
            not bool({row[1]["source_id"], row[1]["target_id"]} & preferred)), 4000),
        "source_refs": section(sorted(knowledge.source_refs.items(), key=lambda row: row[1]["info_unit_id"] not in preferred), 7000),
        "initial_scope": _preview({k: v for k, v in knowledge.initial_scope.items() if k != "evidence_context"}),
        "initial_source_status": {k: v for k, v in knowledge.initial_scope["evidence_context"].items() if k != "items"},
        "activities": section([(str(i), a) for i, a in enumerate(knowledge.activities)], 4500),
        "findings": section([(str(i), f) for i, f in enumerate(knowledge.findings)], 3000),
        "proposals": section([(p["proposal_id"], p) for p in knowledge.proposals], 5000)}
    # Global cap: trim the largest detailed section; progress/budget/recovery stay visible.
    candidates = [result["question_state"]["details"], result["investigation_steps"]["details"], *[v for v in result["knowledge_state"].values()
                  if isinstance(v, dict) and "omitted_ids" in v]]
    while len(json.dumps(result, ensure_ascii=False)) > MAX_CONTEXT_CHARS and any(s["items"] for s in candidates):
        target = max((s for s in candidates if s["items"]), key=lambda s: len(json.dumps(s, ensure_ascii=False)))
        row = target["items"].pop()
        target["omitted_ids"].append(row.get("id") or row.get("proposal_id") or row.get("info_unit_id") or "record")
        target["context_truncated"] = True
    if len(json.dumps(result, ensure_ascii=False)) > MAX_CONTEXT_CHARS:
        raise AppError("investigation_context_limit", "Required task/Issue metadata exceeds the context budget")
    return result
