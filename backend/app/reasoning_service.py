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
    deleted_id = edit.delete_hypothesis_id or edit.delete_gap_id
    if deleted_id:
        collection = state.hypotheses if edit.delete_hypothesis_id else state.gaps
        existing = next((obj for obj in collection if obj.id == deleted_id), None)
        if existing is None:
            raise AppError("unknown_reasoning_object", "Reasoning object is outside the current Issue", 404)
        deleted_ids = {deleted_id}
        affected = set()
        if edit.delete_hypothesis_id:
            # Shared Gaps survive; only Gaps left without a Hypothesis are deleted.
            for gap in list(state.gaps):
                if deleted_id not in gap.hypothesis_ids:
                    continue
                gap.hypothesis_ids = [hid for hid in gap.hypothesis_ids if hid != deleted_id]
                if not gap.hypothesis_ids:
                    state.gaps.remove(gap)
                    deleted_ids.add(gap.id)
                else:
                    gap.revision += 1
                    gap.updated_at, gap.last_edited_by = now, editor
                    affected.update(gap.hypothesis_ids)
            state.links = [link for link in state.links if link.hypothesis_id != deleted_id]
        else:
            affected.update(existing.hypothesis_ids)
        collection.remove(existing)
        for target in state.hypotheses:
            if target.id in affected:
                target.revision += 1
                target.updated_at, target.last_edited_by = now, editor
        canvas = workspace.canvas
        canvas.visible_node_ids = [uid for uid in canvas.visible_node_ids if uid not in deleted_ids]
        canvas.selected_node_ids = [uid for uid in canvas.selected_node_ids if uid not in deleted_ids]
        if canvas.focused_node_id in deleted_ids:
            canvas.focused_node_id = None
        for uid in deleted_ids:
            canvas.node_positions.pop(uid, None)
            canvas.display_levels.pop(uid, None)
        # Keep adoption receipts so retrying an accepted Proposal cannot recreate it.
    elif edit.unlink_id:
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
        values = {**draft.model_dump(exclude={"id", "remove_addressing_info_unit_ids"}), "id": draft.id or f"{prefix}-{uuid4().hex}",
                  "last_edited_by": editor, "proposal_key": proposal_key or (existing.proposal_key if existing else None)}
        if edit.gap:
            added, removed = set(draft.addressing_info_unit_ids), set(draft.remove_addressing_info_unit_ids)
            if added & removed:
                raise AppError("invalid_gap_addressing_edit", "The same material cannot be added and removed in one edit")
            if not added <= {unit.id for unit in case.info_units}:
                raise AppError("unknown_reasoning_evidence", "Addressing material must belong to the current case")
            if existing:
                if not removed <= set(existing.addressing_info_unit_ids):
                    raise AppError("unknown_gap_addressing", "Removed material must already address this Gap")
                if "basis_info_unit_ids" not in draft.model_fields_set:
                    values["basis_info_unit_ids"] = list(existing.basis_info_unit_ids)
                values["addressing_info_unit_ids"] = [uid for uid in dict.fromkeys([
                    *existing.addressing_info_unit_ids, *draft.addressing_info_unit_ids]) if uid not in removed]
                if "status" not in draft.model_fields_set:
                    values["status"] = existing.status
            elif added or removed:
                raise AppError("gap_addressing_requires_existing", "Create the Gap before adding addressing material")
        # Compare the final merged data, not the submitted append list or editor.
        unchanged_gap = bool(edit.gap and existing and all(values[field] == getattr(existing, field) for field in (
            "content", "hypothesis_ids", "basis_info_unit_ids", "addressing_info_unit_ids", "status")))
        if unchanged_gap:
            obj = existing
        elif edit.link:
            if any(link.hypothesis_id == draft.hypothesis_id and link.info_unit_id == draft.info_unit_id
                   and link.id != draft.id for link in collection):
                raise AppError("duplicate_evidence_link", "This InfoUnit is already linked to the Hypothesis", 409)
            values["origin"] = existing.origin if existing else "agent" if proposal_key else "user"
            obj = EvidenceLink(**values)
        else:
            values.update(revision=existing.revision + 1 if existing else 1, updated_at=now)
            obj = (Hypothesis if edit.hypothesis else EvidenceGap)(**values)
        if not unchanged_gap:
            if existing:
                collection[collection.index(existing)] = obj
            else:
                collection.append(obj)
        if (edit.link or edit.gap) and not unchanged_gap:
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
        evidence_ids = ([obj.info_unit_id] if edit.link else
                        list(dict.fromkeys([*obj.basis_info_unit_ids, *obj.addressing_info_unit_ids])) if edit.gap else [])
        for index, uid in enumerate(evidence_ids):
            if uid not in workspace.canvas.visible_node_ids:
                workspace.canvas.visible_node_ids.append(uid)
                workspace.canvas.node_positions.setdefault(uid, Point(x=120, y=120 + index * 180))
    workspace.reasoning = ReasoningState.model_validate(state.model_dump())
    known = {u.id for u in case.info_units}
    if any(link.info_unit_id not in known for link in state.links) or any(
            not set(gap.basis_info_unit_ids + gap.addressing_info_unit_ids) <= known for gap in state.gaps):
        raise AppError("unknown_reasoning_evidence", "Evidence must belong to the current case")
    current.active_issue = edit.issue
    if persist:
        persist_issue_workspaces(case, current)
    return current, deleted_id or (obj.id if obj else edit.unlink_id)
