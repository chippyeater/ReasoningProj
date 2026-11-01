"""Adopt a server-audited suggestion, never arbitrary client-authored evidence."""
import hashlib
import json

from app.case_store import artifact_path
from app.errors import AppError
from app.issue_workspaces import get_issue_workspaces, persist_issue_workspaces
from app.reasoning_models import GapEdit, HypothesisEdit, LinkEdit, ReasoningEdit, ReasoningState, PROPOSAL_ADAPTER, proposal_material_ids
from app.reasoning_service import edit_reasoning


def evidence_hash(case, ids):
    units = {unit.id: unit.model_dump(mode="json") for unit in case.info_units}
    return hashlib.sha256(json.dumps([units.get(uid) for uid in ids], sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def proposal_receipt(case, issue_key, context, proposals):
    if issue_key is None:
        raise AppError("invalid_reasoning_proposal", "No Issue is bound to the proposal")
    reasoning = get_issue_workspaces(case).workspaces[issue_key].reasoning
    gap_ids = {p.gap_id for p in proposals if p.kind == "evidence_gap_update"}
    gaps = [g for g in reasoning.gaps if g.id in gap_ids]
    hypothesis_ids = {h["id"] for h in context.get("reasoning", {}).get("hypotheses", [])}
    hypothesis_ids.update(hid for g in gaps for hid in g.hypothesis_ids)
    targets = ReasoningState(hypotheses=[h for h in reasoning.hypotheses if h.id in hypothesis_ids], gaps=gaps)
    return {"issue": issue_key,
            "target_revisions": {h["id"]: h["revision"] for h in context.get("reasoning", {}).get("hypotheses", [])},
            "target_gap_revisions": {g["id"]: g["revision"] for g in context.get("reasoning", {}).get("gaps", [])},
            # Full target values stay in the server receipt, outside the bounded model context.
            "target_state": targets.model_dump(mode="json"),
            "evidence_hashes": [evidence_hash(case, proposal_material_ids(p)) for p in proposals]}


def validate_proposal_dependencies(proposals):
    """Only Gap/Link -> new Hypothesis, addressed within this exact ordered run."""
    ids = [p.proposal_id for p in proposals if p.proposal_id is not None]
    if len(ids) != len(set(ids)):
        raise AppError("invalid_reasoning_proposal", "Duplicate proposal IDs in run")
    for index, proposal in enumerate(proposals):
        targets = [("hypothesis_proposal_indices", offset, target) for offset, target in enumerate(proposal.hypothesis_proposal_indices)]
        targets.extend(("hypothesis_proposal_ids", offset, next((i for i, p in enumerate(proposals) if p.proposal_id == pid), len(proposals)))
                       for offset, pid in enumerate(proposal.hypothesis_proposal_ids))
        for field, offset, target in targets:
            if target >= len(proposals) or proposals[target].kind != "hypothesis":
                raise AppError("invalid_reasoning_proposal", "Target must be a Hypothesis proposal in this run", details={
                    "category": "proposal_semantics", "errors": [{
                        "loc": ["proposals", index, field, offset],
                        "code": "invalid_hypothesis_proposal_target",
                        "message": "Index must identify a Hypothesis proposal in the same run"}]})


def proposal_key(run_id, proposals, index):
    return f"{run_id}:{proposals[index].proposal_id or index}"


def dependency_indices(proposal, proposals):
    return [*proposal.hypothesis_proposal_indices,
            *(next(i for i, parent in enumerate(proposals) if parent.proposal_id == pid) for pid in proposal.hypothesis_proposal_ids)]


def _proposal_edit(proposal, hypothesis_ids, state, issue, position=None):
    edit = ReasoningEdit.model_construct(issue=issue, position=position)
    if proposal.kind == "hypothesis":
        edit.hypothesis = HypothesisEdit(content=proposal.content)
    elif proposal.kind == "evidence_gap":
        edit.gap = GapEdit(content=proposal.content, hypothesis_ids=hypothesis_ids, basis_info_unit_ids=proposal.basis_info_unit_ids)
    elif proposal.kind == "evidence_link":
        edit.link = LinkEdit(hypothesis_id=hypothesis_ids[0], info_unit_id=proposal.info_unit_ids[0],
                             meaning=proposal.meaning, rationale=proposal.rationale)
    elif proposal.kind == "evidence_gap_update":
        gap = next(g for g in state.gaps if g.id == proposal.gap_id)
        edit.gap = GapEdit(id=gap.id, content=gap.content, hypothesis_ids=list(gap.hypothesis_ids),
                           addressing_info_unit_ids=proposal.addressing_info_unit_ids, status=proposal.status)
    else:
        target = next(h for h in state.hypotheses if h.id == hypothesis_ids[0])
        edit.hypothesis = HypothesisEdit(id=target.id, content=target.content, judgment=proposal.judgment, work_status=target.work_status)
    return edit


def _expected_reasoning(case, current, issue, run_id, proposals, receipt):
    """Replay only this run's additive approvals with the real edit/revision rules.

    accepted_proposals preserves adoption order. Replaying on a detached workspace
    accounts for Gap no-ops without crediting user edits or another run's writes.
    """
    shadow = current.model_copy(deep=True)
    workspace = shadow.workspaces[issue]
    workspace.reasoning = ReasoningState.model_validate(receipt.get("target_state", {
        # Existing receipts contain revision baselines but no full Gap values.
        # Hypothesis increments remain provable; Gap append no-ops do not.
        "hypotheses": [{**h.model_dump(), "revision": receipt["target_revisions"][h.id]}
                       for h in workspace.reasoning.hypotheses if h.id in receipt["target_revisions"]]}))
    for obj in [*workspace.reasoning.hypotheses, *workspace.reasoning.gaps]:
        revisions = receipt["target_revisions"] if hasattr(obj, "judgment") else receipt.get("target_gap_revisions", {})
        if obj.revision != revisions.get(obj.id, obj.revision):
            raise AppError("stale_reasoning_proposal", "Reasoning changed during investigation", 409)
    indices = {proposal_key(run_id, proposals, i): i for i in range(len(proposals))}
    for key, object_id in current.workspaces[issue].reasoning.accepted_proposals.items():
        if key not in indices:
            continue
        proposal = proposals[indices[key]]
        # A judgment change is not an independent evidence addition.
        if proposal.kind == "hypothesis_status":
            continue
        state = workspace.reasoning
        hypothesis_ids = [*proposal.hypothesis_ids, *(state.accepted_proposals[proposal_key(run_id, proposals, i)]
                          for i in dependency_indices(proposal, proposals))]
        edit = _proposal_edit(proposal, hypothesis_ids, state, issue)
        shadow, generated_id = edit_reasoning(case, edit, proposal_key=key, current=shadow, persist=False)
        workspace = shadow.workspaces[issue]
        # Replay allocates temporary IDs; preserve the IDs of actual approvals.
        for obj in [*workspace.reasoning.hypotheses, *workspace.reasoning.gaps, *workspace.reasoning.links]:
            if obj.id == generated_id:
                obj.id = object_id
                break
        workspace.reasoning.accepted_proposals[key] = object_id
    return workspace.reasoning


def _same_target(actual, expected):
    # Replay timestamps are intentionally fresh; revisions and all data must match.
    return actual.model_dump(exclude={"updated_at"}) == expected.model_dump(exclude={"updated_at"})


def accept_proposal(case, request):
    current = get_issue_workspaces(case)
    if request.issue not in current.workspaces:
        raise AppError("unknown_issue", "Issue does not belong to the case")
    try:
        audit = json.loads(artifact_path(case.case_id, f"runs/{request.run_id}.output.json").read_text(encoding="utf-8"))
        receipt = audit["proposal_receipt"]
        if (audit["status"] not in {"success", "partial"} or receipt["issue"] != request.issue
                or audit["run_id"] != request.run_id or audit["result"]["run_id"] != request.run_id):
            raise ValueError("Proposal belongs to a different Issue or run")
        proposals = [PROPOSAL_ADAPTER.validate_python(p) for p in audit["result"]["proposals"]]
        index = request.proposal_index if request.proposal_id is None else next(
            (i for i, p in enumerate(proposals) if p.proposal_id == request.proposal_id), len(proposals))
        proposal = proposals[index]
    except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
        raise AppError("invalid_reasoning_proposal", "No validated suggestion exists at this reference", 409) from exc
    try:
        validate_proposal_dependencies(proposals)
    except AppError as exc:
        exc.status_code = 409
        raise
    state = current.workspaces[request.issue].reasoning
    key = proposal_key(request.run_id, proposals, index)
    if key in state.accepted_proposals:
        return {"workspaces": current, "object_id": state.accepted_proposals[key], "already_accepted": True}
    if evidence_hash(case, proposal_material_ids(proposal)) != receipt["evidence_hashes"][index]:
        raise AppError("stale_reasoning_proposal", "Evidence changed; investigate again before adopting", 409)
    targets = {h.id: h for h in state.hypotheses}
    hypothesis_ids = list(proposal.hypothesis_ids)
    for parent_index in dependency_indices(proposal, proposals):
        parent_key = proposal_key(request.run_id, proposals, parent_index)
        hid = state.accepted_proposals.get(parent_key)
        if hid not in targets or targets[hid].proposal_key != parent_key:
            raise AppError("proposal_dependency_not_accepted", "Adopt the referenced Hypothesis in this run first", 409)
        parent = proposals[parent_index]
        if evidence_hash(case, parent.info_unit_ids) != receipt["evidence_hashes"][parent_index]:
            raise AppError("stale_reasoning_proposal", "Hypothesis evidence changed; investigate again before adopting", 409)
        hypothesis_ids.append(hid)
    try:
        expected = _expected_reasoning(case, current, request.issue, request.run_id, proposals, receipt)
    except (AppError, ValueError, KeyError, StopIteration) as exc:
        raise AppError("stale_reasoning_proposal", "Approved reasoning no longer matches this run", 409) from exc
    expected_hypotheses = {h.id: h for h in expected.hypotheses}
    if proposal.kind == "evidence_gap_update":
        gap = next((g for g in state.gaps if g.id == proposal.gap_id), None)
        expected_gap = next((g for g in expected.gaps if g.id == proposal.gap_id), None)
        if gap is None or (not _same_target(gap, expected_gap) if expected_gap else
                           gap.revision != receipt.get("target_gap_revisions", {}).get(proposal.gap_id)):
            raise AppError("stale_reasoning_proposal", "Target Gap changed; investigate again before adopting", 409)
        for i, sibling in enumerate(proposals):
            if (sibling.kind == "evidence_gap_update" and sibling.gap_id == gap.id
                    and proposal_key(request.run_id, proposals, i) in state.accepted_proposals
                    and sibling.status != proposal.status):
                raise AppError("stale_reasoning_proposal", "Gap status suggestions conflict", 409)
        hypothesis_ids = list(gap.hypothesis_ids)
    for hid in hypothesis_ids:
        target, baseline = targets.get(hid), expected_hypotheses.get(hid)
        if target is None or (not _same_target(target, baseline) if baseline else
                              target.revision != receipt["target_revisions"].get(hid)):
            raise AppError("stale_reasoning_proposal", "User reasoning changed; investigate again before adopting", 409)
        # A judgment proposal must still refer to the original evidence state.
        if proposal.kind == "hypothesis_status" and target.revision != receipt["target_revisions"].get(hid):
            raise AppError("stale_reasoning_proposal", "Evidence reasoning changed before judgment approval", 409)
    # Validate the actual siblings whose effects explain the target revisions.
    objects = {obj.id: obj for obj in [*state.gaps, *state.links]}
    for obj in [*expected.gaps, *expected.links]:
        affected = obj.hypothesis_ids if hasattr(obj, "hypothesis_ids") else [obj.hypothesis_id]
        if set(affected) & set(hypothesis_ids) and (obj.id not in objects or not _same_target(objects[obj.id], obj)):
            raise AppError("stale_reasoning_proposal", "Related evidence reasoning changed", 409)
    for i, sibling in enumerate(proposals):
        object_id = state.accepted_proposals.get(proposal_key(request.run_id, proposals, i))
        obj = objects.get(object_id)
        affected = (obj.hypothesis_ids if hasattr(obj, "hypothesis_ids") else [obj.hypothesis_id]) if obj else []
        if set(affected) & set(hypothesis_ids) and evidence_hash(case, proposal_material_ids(sibling)) != receipt["evidence_hashes"][i]:
            raise AppError("stale_reasoning_proposal", "Approved sibling evidence changed", 409)
    # Gap updates retain their own associations rather than receiving target IDs.
    edit = _proposal_edit(proposal, hypothesis_ids, state, request.issue, request.position)
    current, object_id = edit_reasoning(case, edit, proposal_key=key, current=current, persist=False)
    current.workspaces[request.issue].reasoning.accepted_proposals[key] = object_id
    persist_issue_workspaces(case, current)
    return {"workspaces": current, "object_id": object_id, "already_accepted": False}
