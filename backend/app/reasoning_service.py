"""Explicit user edits; no model or business-data writes."""
from datetime import datetime, timezone
from uuid import uuid4

from app.errors import AppError
from app.issue_workspaces import get_issue_workspaces, persist_issue_workspaces
from app.reasoning_models import EvidenceGap, EvidenceLink, Hypothesis, ReasoningEdit, ReasoningState
from app.schemas import CaseData, Point


def edit_reasoning(case: CaseData, edit: ReasoningEdit, *, proposal_key: str | None = None,
                   current=None, persist: bool = True):
    current = current if current is not None else get_issue_workspaces(case)
    if edit.issue not in current.workspaces:
        raise AppError("unknown_issue", "Issue does not belong to the case")
    workspace = current.workspaces[edit.issue]
    state = workspace.reasoning
    editor = "agent_accepted" if proposal_key else "user"
    now = datetime.now(timezone.utc).isoformat()
    collection, obj = None, None
    draft = edit.hypothesis or edit.gap or edit.link
    if edit.unlink_id:
        existing = next((link for link in state.links if link.id == edit.unlink_id), None)
        if existing is None:
            raise AppError("unknown_reasoning_link", "Evidence link no longer exists", 404)
        state.links.remove(existing)
        # Removing a link changes its Hypothesis' reasoning, invalidating old suggestions.
        target = next(h for h in state.hypotheses if h.id == existing.hypothesis_id)
        target.revision += 1
        target.updated_at, target.last_edited_by = now, editor
    else:
        collection = state.hypotheses if edit.hypothesis else state.gaps if edit.gap else state.links
        existing = next((obj for obj in collection if obj.id == draft.id), None) if draft.id else None
        if draft.id and existing is None:
            raise AppError("unknown_reasoning_object", "Reasoning object is outside the current Issue", 404)
        prefix = "hyp" if edit.hypothesis else "gap" if edit.gap else "elink"
        values = {**draft.model_dump(exclude={"id"}), "id": draft.id or f"{prefix}-{uuid4().hex}",
                  "last_edited_by": editor, "proposal_key": proposal_key or (existing.proposal_key if existing else None)}
        if edit.link:
            if any(link.hypothesis_id == draft.hypothesis_id and link.info_unit_id == draft.info_unit_id
                   and link.id != draft.id for link in collection):
                raise AppError("duplicate_evidence_link", "This InfoUnit is already linked to the Hypothesis", 409)
            values["origin"] = existing.origin if existing else "agent" if proposal_key else "user"
            obj = EvidenceLink(**values)
        else:
            values.update(revision=existing.revision + 1 if existing else 1, updated_at=now)
            obj = (Hypothesis if edit.hypothesis else EvidenceGap)(**values)
        if existing:
            collection[collection.index(existing)] = obj
        else:
            collection.append(obj)
        if edit.link or edit.gap:
            affected = {obj.hypothesis_id} if edit.link else set(obj.hypothesis_ids)
            if existing:
                affected.update([existing.hypothesis_id] if edit.link else existing.hypothesis_ids)
            for target in state.hypotheses:
                if target.id in affected:
                    target.revision += 1
                    target.updated_at, target.last_edited_by = now, editor
        if not edit.link:
            canvas = workspace.canvas
            if obj.id not in canvas.visible_node_ids:
                canvas.visible_node_ids.append(obj.id)
            if obj.id not in canvas.node_positions:
                canvas.node_positions[obj.id] = edit.position or Point(x=800, y=200)
        # Show linked evidence without copying it. SourceRefs stay case-owned.
        evidence_ids = [obj.info_unit_id] if edit.link else obj.info_unit_ids if edit.gap else []
        for index, uid in enumerate(evidence_ids):
            if uid not in workspace.canvas.visible_node_ids:
                workspace.canvas.visible_node_ids.append(uid)
                workspace.canvas.node_positions.setdefault(uid, Point(x=120, y=120 + index * 180))
    workspace.reasoning = ReasoningState.model_validate(state.model_dump())
    known = {u.id for u in case.info_units}
    if any(link.info_unit_id not in known for link in state.links) or any(not set(gap.info_unit_ids) <= known for gap in state.gaps):
        raise AppError("unknown_reasoning_evidence", "Evidence must belong to the current case")
    current.active_issue = edit.issue
    if persist:
        persist_issue_workspaces(case, current)
    return current, obj.id if obj else edit.unlink_id
