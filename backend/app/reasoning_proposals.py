"""Adopt a server-audited suggestion, never arbitrary client-authored evidence."""
import hashlib
import json

from app.case_store import artifact_path
from app.errors import AppError
from app.issue_workspaces import get_issue_workspaces, persist_issue_workspaces
from app.reasoning_models import GapEdit, HypothesisEdit, LinkEdit, ReasoningEdit, ReasoningProposal
from app.reasoning_service import edit_reasoning


def evidence_hash(case, ids):
    units = {unit.id: unit.model_dump(mode="json") for unit in case.info_units}
    return hashlib.sha256(json.dumps([units.get(uid) for uid in ids], sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def proposal_receipt(case, issue_key, context, proposals):
    if issue_key is None:
        raise AppError("invalid_reasoning_proposal", "No Issue is bound to the proposal")
    return {"issue": issue_key,
            "target_revisions": {h["id"]: h["revision"] for h in context.get("reasoning", {}).get("hypotheses", [])},
            "evidence_hashes": [evidence_hash(case, p.info_unit_ids) for p in proposals]}


def validate_proposal_dependencies(proposals):
    """Only Gap -> new Hypothesis, addressed within this exact ordered run."""
    for index, proposal in enumerate(proposals):
        for offset, target in enumerate(proposal.hypothesis_proposal_indices):
            if target >= len(proposals) or proposals[target].kind != "hypothesis":
                raise AppError("invalid_reasoning_proposal", "Gap target must be a Hypothesis proposal in this run", details={
                    "category": "proposal_semantics", "errors": [{
                        "loc": ["proposals", index, "hypothesis_proposal_indices", offset],
                        "code": "invalid_hypothesis_proposal_target",
                        "message": "Index must identify a Hypothesis proposal in the same run"}]})


def accept_proposal(case, request):
    current = get_issue_workspaces(case)
    if request.issue not in current.workspaces:
        raise AppError("unknown_issue", "Issue does not belong to the case")
    try:
        audit = json.loads(artifact_path(case.case_id, f"runs/{request.run_id}.output.json").read_text(encoding="utf-8"))
        receipt = audit["proposal_receipt"]
        if (audit["status"] != "success" or receipt["issue"] != request.issue
                or audit["run_id"] != request.run_id or audit["result"]["run_id"] != request.run_id):
            raise ValueError("Proposal belongs to a different Issue or run")
        proposals = [ReasoningProposal.model_validate(p) for p in audit["result"]["proposals"]]
        proposal = proposals[request.proposal_index]
    except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
        raise AppError("invalid_reasoning_proposal", "No validated suggestion exists at this reference", 409) from exc
    try:
        validate_proposal_dependencies(proposals)
    except AppError as exc:
        exc.status_code = 409
        raise
    state = current.workspaces[request.issue].reasoning
    key = f"{request.run_id}:{request.proposal_index}"
    if key in state.accepted_proposals:
        return {"workspaces": current, "object_id": state.accepted_proposals[key], "already_accepted": True}
    if evidence_hash(case, proposal.info_unit_ids) != receipt["evidence_hashes"][request.proposal_index]:
        raise AppError("stale_reasoning_proposal", "Evidence changed; investigate again before adopting", 409)
    targets = {h.id: h for h in state.hypotheses}
    for hid in proposal.hypothesis_ids:
        if hid not in targets or targets[hid].revision != receipt["target_revisions"].get(hid):
            raise AppError("stale_reasoning_proposal", "User reasoning changed; investigate again before adopting", 409)
    hypothesis_ids = list(proposal.hypothesis_ids)
    for index in proposal.hypothesis_proposal_indices:
        parent_key = f"{request.run_id}:{index}"
        hid = state.accepted_proposals.get(parent_key)
        if hid not in targets or targets[hid].proposal_key != parent_key:
            raise AppError("proposal_dependency_not_accepted", "Adopt the referenced Hypothesis in this run first", 409)
        parent = proposals[index]  # Type and same-run bounds checked above, including at adoption.
        if evidence_hash(case, parent.info_unit_ids) != receipt["evidence_hashes"][index]:
            raise AppError("stale_reasoning_proposal", "Hypothesis evidence changed; investigate again before adopting", 409)
        # Each already-adopted sibling Gap advances the parent's revision once.
        # All other edits (including later user edits to a Gap) still invalidate it.
        adopted_gaps = {g.id: g for g in state.gaps}
        sibling_count = sum(
            1 for sibling_index, sibling in enumerate(proposals)
            if index in sibling.hypothesis_proposal_indices
            and (gap := adopted_gaps.get(state.accepted_proposals.get(f"{request.run_id}:{sibling_index}")))
            and gap.proposal_key == f"{request.run_id}:{sibling_index}" and hid in gap.hypothesis_ids)
        target = targets[hid]
        if (target.revision != 1 + sibling_count or target.content != parent.content
                or target.judgment != "unverified" or target.work_status != "active"):
            raise AppError("stale_reasoning_proposal", "Accepted Hypothesis changed; investigate again before adopting", 409)
        hypothesis_ids.append(hid)
    edit = ReasoningEdit.model_construct(issue=request.issue, position=request.position)
    if proposal.kind == "hypothesis":
        edit.hypothesis = HypothesisEdit(content=proposal.content)
    elif proposal.kind == "evidence_gap":
        edit.gap = GapEdit(content=proposal.content, hypothesis_ids=hypothesis_ids, info_unit_ids=proposal.info_unit_ids)
    elif proposal.kind == "evidence_link":
        edit.link = LinkEdit(hypothesis_id=proposal.hypothesis_ids[0], info_unit_id=proposal.info_unit_ids[0],
                             meaning=proposal.meaning, rationale=proposal.rationale)
    else:
        target = targets[proposal.hypothesis_ids[0]]
        edit.hypothesis = HypothesisEdit(id=target.id, content=target.content, judgment=proposal.judgment, work_status=target.work_status)
    current, object_id = edit_reasoning(case, edit, proposal_key=key, current=current, persist=False)
    current.workspaces[request.issue].reasoning.accepted_proposals[key] = object_id
    persist_issue_workspaces(case, current)
    return {"workspaces": current, "object_id": object_id, "already_accepted": False}
