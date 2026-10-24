"""Run-local working state; domain records remain existing tool projections."""
from __future__ import annotations

from copy import deepcopy
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from app.errors import AppError

Text = Annotated[str, Field(min_length=1, max_length=600, pattern=r"\S")]
Identifier = Annotated[str, Field(min_length=1, max_length=80, pattern=r"\S")]
MAX_QUESTIONS = 20
MAX_CONTEXT_CHARS = 48000  # Deterministic character proxy, independent of provider tokenizer.


class StateModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class EvidencePointer(StateModel):
    info_unit_id: Text
    source_ref_index: Annotated[int, Field(ge=0)]


class PlannedAction(StateModel):
    id: Identifier
    kind: Literal["hypothesis", "evidence_link", "evidence_gap", "hypothesis_status"]
    description: Text
    proposal_id: Text | None
    omission_reason: Text | None

    @model_validator(mode="after")
    def disposition(self):
        if self.proposal_id is not None and self.omission_reason is not None:
            raise ValueError("Choose a submitted proposal or an omission reason")
        return self


class Question(StateModel):
    id: Identifier
    question: Annotated[str, Field(min_length=1, max_length=2000, pattern=r"\S")]
    status: Literal["pending", "in_progress", "answered", "blocked"]
    related_info_unit_ids: Annotated[list[Text], Field(max_length=80)]
    evidence_refs: Annotated[list[EvidencePointer], Field(max_length=64)]
    finding: Text | None
    block_reason: Text | None
    block_kind: Literal["evidence_missing", "tool_failure", "unsupported", "budget_exhausted"] | None
    planned_actions: Annotated[list[PlannedAction], Field(max_length=6)]

    @model_validator(mode="after")
    def consistent_status(self):
        if self.status == "blocked":
            if not self.block_reason or not self.block_kind:
                raise ValueError("Blocked questions require a specific block_reason and block_kind")
        elif self.block_reason is not None or self.block_kind is not None:
            raise ValueError("Only blocked questions may have blocking fields")
        if self.status == "answered" and (not self.finding or not (self.related_info_unit_ids or self.evidence_refs)):
            raise ValueError("Answered questions require a finding and obtained materials")
        if len({p.id for p in self.planned_actions}) != len(self.planned_actions):
            raise ValueError("Duplicate planned action IDs")
        return self


class QuestionState:
    def __init__(self, goal):
        self.items = {"Q1": Question(id="Q1", question=goal, status="pending", related_info_unit_ids=[],
            evidence_refs=[], finding=None, block_reason=None, block_kind=None, planned_actions=[])}
        self.activities = {"Q1": []}

    def dump(self):
        return {"questions": [q.model_dump(mode="json") for q in self.items.values()],
                "activities": deepcopy(self.activities), "current_question_ids": self.resolve(None)}

    def resolve(self, ids):
        if ids is None:
            ids = [q.id for q in self.items.values() if q.status == "in_progress"]
            ids = ids or [q.id for q in self.items.values() if q.status == "pending"][:1] or ["Q1"]
        if not isinstance(ids, list) or not ids or len(ids) > MAX_QUESTIONS or any(not isinstance(i, str) for i in ids):
            raise AppError("invalid_question_reference", "Supply a nonempty list of current Question IDs")
        if len(set(ids)) != len(ids) or not set(ids) <= self.items.keys():
            raise AppError("invalid_question_reference", "Question IDs must be unique and already registered")
        return list(ids)

    def update(self, updates, case, seen, context, proposals):
        from app.investigation_actions import artifacts
        parsed = TypeAdapter(list[Question]).validate_python(updates, strict=True)
        if not 1 <= len(parsed) <= MAX_QUESTIONS or len({q.id for q in parsed}) != len(parsed):
            raise AppError("invalid_questions", "Submit one to twenty unique Questions per batch")
        draft = deepcopy(self.items)
        lookup = {p.proposal_id: p for p in proposals}
        for q in parsed:
            # Preserve registered commitments when adjusting/splitting a task.
            previous = draft.get(q.id)
            if previous and not {p.id for p in previous.planned_actions} <= {p.id for p in q.planned_actions}:
                raise AppError("unresolved_plan", "Do not delete planned actions; submit them or record omission_reason")
            for plan in q.planned_actions:
                old = next((p for p in previous.planned_actions if p.id == plan.id), None) if previous else None
                if old and (old.kind, old.description) != (plan.kind, plan.description):
                    raise AppError("unresolved_plan", "Keep registered action identity; explain omission before adding a replacement")
                if plan.proposal_id:
                    proposal = lookup.get(plan.proposal_id)
                    if not proposal or proposal.kind != plan.kind or (
                            plan.kind in {"hypothesis", "evidence_gap"} and proposal.content != plan.description):
                        raise AppError("invalid_plan_reference", "Plan must reference its actual same-run Proposal of the matching kind/content")
            if q.related_info_unit_ids or q.evidence_refs:
                artifacts(case, seen, context, "Question basis", [], [{"statement": q.finding or q.question[:600],
                    "info_unit_ids": q.related_info_unit_ids, "relation_ids": [],
                    "evidence_refs": [r.model_dump() for r in q.evidence_refs]}], [], "evidence_insufficient")
            draft[q.id] = q
        if len(draft) > MAX_QUESTIONS:
            raise AppError("question_limit", "At most twenty run-local Questions")
        submitted = [p.proposal_id for q in draft.values() for p in q.planned_actions if p.proposal_id]
        if len(submitted) != len(set(submitted)):
            raise AppError("invalid_plan_reference", "One Proposal cannot discharge multiple planned actions")
        self.items = draft
        for q in parsed:
            self.activities.setdefault(q.id, [])
        return {"status": "ok", "updated_question_ids": [q.id for q in parsed]}

    def open_questions(self):
        return [(q.question + ("：" + q.block_reason if q.block_reason else ""))[:600]
                for q in self.items.values() if q.status != "answered"]

    def record(self, activity_index, ids):
        for qid in ids:
            self.activities[qid].append(activity_index)


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

    def merge(self, name, args, response, question_ids):
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
        activity = {"tool": name, "arguments": deepcopy(args), "question_ids": list(question_ids),
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
            "block_kind": q.block_kind, "block_reason": _preview(q.block_reason, 240),
            "pending_action_ids": [p.id for p in q.planned_actions if not p.proposal_id and not p.omission_reason]}
            for q in questions.items.values()],
        "details": section(sorted([(q["id"], q) for q in qdump["questions"]], key=lambda row: row[0] not in current), 12000)}
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
    candidates = [result["question_state"]["details"], *[v for v in result["knowledge_state"].values()
                  if isinstance(v, dict) and "omitted_ids" in v]]
    while len(json.dumps(result, ensure_ascii=False)) > MAX_CONTEXT_CHARS and any(s["items"] for s in candidates):
        target = max((s for s in candidates if s["items"]), key=lambda s: len(json.dumps(s, ensure_ascii=False)))
        row = target["items"].pop()
        target["omitted_ids"].append(row.get("id") or row.get("proposal_id") or row.get("info_unit_id") or "record")
        target["context_truncated"] = True
    if len(json.dumps(result, ensure_ascii=False)) > MAX_CONTEXT_CHARS:
        raise AppError("investigation_context_limit", "Required task/Issue metadata exceeds the context budget")
    return result
