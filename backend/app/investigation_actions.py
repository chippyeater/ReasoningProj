"""Validated investigation artifacts. These tools never write the Issue workspace."""
import json
import re
from uuid import uuid4

from app.errors import AppError
from app.reasoning_models import PROPOSAL_ADAPTER, proposal_material_ids


def _check_duplicate(proposal, context, proposals, case):
    """A conservative text guard, alongside the model's semantic comparison.

    Compare the stated proposition/question, not rationale, citation order or
    chosen associations. Never merge, rewrite or adopt an existing object.
    """
    if proposal.kind not in {"hypothesis", "evidence_gap"}:
        return
    from app.investigation_tools import _normalized
    from app.issue_workspaces import get_issue_workspaces

    def key(content):
        text = re.sub(r"\s+", " ", _normalized(content).strip())
        # Display numbering is not part of the proposition; preserve all
        # subjects, conditions and numerical facts after the label.
        text = re.sub(r"^(?:h\d+|假设\s*\d+)\s*[:：]\s*", "", text)
        text = re.sub(r"^(确认|核实|核对|查明|confirm\s+|verify\s+)", "", text)
        # Keep all clause/parenthesis content and grouping. Numeric separators,
        # signs, ratios and comparison operators may change the proposition.
        def separator(match):
            left, right = text[:match.start()].rstrip(), text[match.end():].lstrip()
            numeric = left and left[-1].isdigit() or right and right[0].isdigit()
            comparison = match[0] == "!" and right.startswith("=")
            return match[0] if numeric or comparison else ""
        text = re.sub(r"""[.,:;!?'"“”‘’。、]""", separator, text)
        # Latin word boundaries distinguish e.g. 'not able' from 'notable'.
        return re.sub(r"(?<![a-z0-9])\s+|\s+(?![a-z0-9])", "", text)

    section = "hypotheses" if proposal.kind == "hypothesis" else "gaps"
    formal = list((context or {}).get("reasoning", {}).get(section, []))
    # The model receives a bounded workspace projection. Check omitted formal
    # objects too, without injecting the full workspace into model context.
    workspace = get_issue_workspaces(case).workspaces.get((context or {}).get("current_issue"))
    if workspace:
        formal = [obj.model_dump(mode="json") for obj in getattr(workspace.reasoning, section)] + formal
    candidates = [("reasoning", obj) for obj in formal if not obj.get("content_truncated")]
    candidates += [("proposals", p.model_dump(mode="json")) for p in proposals if p.kind == proposal.kind]
    current = key(proposal.content)
    for source, obj in candidates:
        previous = key(obj["content"])
        duplicate = current == previous
        if duplicate:
            existing = {"source": source, "kind": proposal.kind,
                        "id": obj.get("proposal_id") or obj["id"], "content": obj["content"]}
            raise AppError("duplicate_proposal", "This Hypothesis or missing condition already exists", details={
                "category": "proposal_semantics", "errors": [{"loc": ["content"], "code": "duplicate_proposal",
                    "message": "Reuse the existing object instead of creating a synonymous Proposal",
                    "existing_object": existing}]})


def artifacts(case, seen, context, goal, proposals, questions, status):
    """Reuse citation/kind validation; no final model JSON is involved."""
    from app.investigation_agent import _validate_result
    units = []
    for item in (p.model_dump(mode="json") for p in proposals):
        material_ids = (item["basis_info_unit_ids"] if item.get("kind") == "evidence_gap" else
                        item["addressing_info_unit_ids"] if item.get("kind") == "evidence_gap_update" else item["info_unit_ids"])
        for uid in material_ids:
            if uid not in units:
                units.append(uid)
    return _validate_result(json.dumps({
        "goal": goal, "relevant_info_unit_ids": units,
        "relevant_relation_ids": [], "evidence_refs": [], "open_questions": questions,
        "completion_status": status, "proposals": [p.model_dump(mode="json") for p in proposals],
    }, ensure_ascii=False), goal, case, seen, context)


def _propose(case, seen, context, proposals, kind, **values):
    if len(proposals) >= 6:
        raise AppError("proposal_limit", "At most six pending proposals per run")
    proposal = PROPOSAL_ADAPTER.validate_python({
        "kind": kind, "content": None, "hypothesis_ids": [], "meaning": None, "judgment": None,
        **values, "proposal_id": f"proposal-{uuid4().hex}",
    })
    # No append until every citation and current-run dependency has passed.
    artifacts(case, seen, context, "validate pending suggestion", [*proposals, proposal], [], "evidence_insufficient")
    _check_duplicate(proposal, context, proposals, case)
    from app.issue_workspaces import get_issue_workspaces
    from app.reasoning_proposals import stage_proposals
    issue = context["current_issue"]
    current = get_issue_workspaces(case)
    candidate, _ = stage_proposals(case, issue, [*proposals, proposal], current, "validation")
    if f"validation:{proposal.proposal_id}" not in candidate.workspaces[issue].reasoning.accepted_proposals:
        return {"status": "ok", "pending": False, "no_change": True,
                "message": ("The current/staged Gap status is unchanged and all supplied addressing InfoUnits are already associated. "
                    "Rewording rationale alone does not update a Gap. No Proposal was staged. "
                    "Inspect any unread Goal-relevant existing material, or call finish_investigation if no actual new change is needed. "
                    "Do not resubmit the same operation with different wording."
                    if kind == "evidence_gap_update" else
                    "The requested state already holds. No Proposal was staged; continue or Finish.")}
    proposals.append(proposal)
    return {"status": "ok", "proposal_id": proposal.proposal_id, "pending": True,
            "proposal": proposal.model_dump(mode="json")}


def propose_hypothesis(case, seen, context, proposals, *, content, rationale, info_unit_ids):
    """Create a new, evidence-testable Hypothesis. Use content to state one clear proposition and rationale to explain its basis and uncertainty. Include relevant obtained InfoUnit IDs when available. Returns a proposal_id for referencing the new Hypothesis within the current investigation."""
    return _propose(case, seen, context, proposals, "hypothesis", content=content, rationale=rationale,
                    info_unit_ids=info_unit_ids)


def propose_evidence_link(case, seen, context, proposals, *, hypothesis_id, info_unit_id, meaning, rationale,
                          hypothesis_proposal_ids=None):
    """Create an Evidence Link between a Hypothesis and one obtained InfoUnit. Identify the Hypothesis using its existing hypothesis_id or a proposal_id returned earlier in this investigation. Set meaning to support, oppose, or related, and use rationale to explain the relationship."""
    return _propose(case, seen, context, proposals, "evidence_link", hypothesis_ids=[] if hypothesis_id is None else [hypothesis_id],
                    hypothesis_proposal_ids=[] if hypothesis_proposal_ids is None else hypothesis_proposal_ids,
                    info_unit_ids=[info_unit_id], meaning=meaning, rationale=rationale)


def propose_evidence_gap(case, seen, context, proposals, *, content, hypothesis_ids, hypothesis_proposal_ids,
                         rationale, basis_info_unit_ids):
    """Create an Evidence Gap describing a specific unresolved factual question. Use content to state what needs to be established, rationale to explain its relevance, and hypothesis_targets to associate the question with one or more Hypotheses. Include relevant obtained basis_info_unit_ids when available."""
    return _propose(case, seen, context, proposals, "evidence_gap", content=content, hypothesis_ids=hypothesis_ids,
                    hypothesis_proposal_ids=hypothesis_proposal_ids, rationale=rationale,
                    basis_info_unit_ids=basis_info_unit_ids)


def propose_hypothesis_status(case, seen, context, proposals, *, hypothesis_id, judgment, rationale, info_unit_ids):
    """Update the judgment of an existing Hypothesis to unverified, verified, or refuted. Provide its hypothesis_id, the new judgment, relevant obtained info_unit_ids, and a rationale explaining the evidential basis for the change."""
    return _propose(case, seen, context, proposals, "hypothesis_status", hypothesis_ids=[hypothesis_id],
                    judgment=judgment, rationale=rationale, info_unit_ids=info_unit_ids)


def propose_evidence_gap_update(case, seen, context, proposals, *, gap_id, addressing_info_unit_ids,
                                status, rationale):
    """Update an existing Evidence Gap's status, addressing materials, or both. Use status = resolved when the factual question has been sufficiently answered; otherwise use status = open and explain the remaining uncertainty. Provide addressing_info_unit_ids to associate newly obtained responding materials with the Gap. Submit an update when the status changes or new responding InfoUnits are associated."""
    response = _propose(case, seen, context, proposals, "evidence_gap_update", gap_id=gap_id,
                        addressing_info_unit_ids=addressing_info_unit_ids, status=status, rationale=rationale)
    formal, effective = _hypothesis_states(case, context, proposals)
    from app.issue_workspaces import get_issue_workspaces
    state = get_issue_workspaces(case).workspaces[context["current_issue"]].reasoning
    gap = next(g for g in state.gaps if g.id == gap_id)
    # Only successfully staged changes enter proposals; failed/no-change calls do not.
    updated_hypothesis_ids = {hid for p in proposals if p.kind == "hypothesis_status" for hid in p.hypothesis_ids}
    related_hypotheses = [
        {"hypothesis_id": hid, "content": formal[hid]["content"], "judgment": formal[hid]["judgment"],
         "formal_judgment": formal[hid]["judgment"], "effective_judgment": effective[hid]["judgment"],
         "pending_changes": [p.model_dump(mode="json") for p in proposals
                             if p.kind == "hypothesis_status" and hid in p.hypothesis_ids]}
        for hid in gap.hypothesis_ids if hid not in updated_hypothesis_ids]
    response["next_step"] = {"related_hypotheses": related_hypotheses,
        "instruction": (
            "请检查本次回应 Gap 的证据是否对关联 Hypothesis 的判断产生影响。根据证据实际证明力，判断是否需要更新；判断改变时调用 propose_hypothesis_status，不变时保留原状态，不创建无意义 Proposal。Gap resolved 不自动改变 Hypothesis judgment。"
            if related_hypotheses else
            "没有尚未提交状态更新的关联 Hypothesis，无需继续提交 Hypothesis 状态更新；不要重复提交已有 Proposal。")}
    return response


def _hypothesis_states(case, context, proposals):
    from app.issue_workspaces import get_issue_workspaces
    from app.investigation_state import effective_reasoning
    workspace = get_issue_workspaces(case).workspaces.get((context or {}).get("current_issue"))
    reasoning = workspace.reasoning.model_dump(mode="json") if workspace else {"hypotheses": [], "gaps": [], "links": []}
    effective = effective_reasoning(reasoning, [p.model_dump(mode="json") for p in proposals])
    return ({h["id"]: h for h in reasoning["hypotheses"]},
            {h["id"]: h for h in effective["hypotheses"]})


def finish_investigation(case, seen, context, goal, proposals, *, answer,
                         open_questions, completion_status, answer_basis, knowledge,
                         budget_exhausted=False, read_budget_exhausted=False):
    """Complete the current investigation by submitting answer, answer_basis, open_questions, and completion_status. Keep answer within 1,000 characters. Use answer_basis = case_material for conclusions based on obtained case evidence, case_metadata for case metadata, or general for general knowledge. Set completion_status = completed when the Goal has been sufficiently addressed, evidence_insufficient when critical evidence is missing, budget_exhausted when the investigation budget prevents completion, or unsupported when the Goal exceeds available capabilities. Include relevant unresolved matters in open_questions."""
    from app.investigation_agent import _validate_summary
    def invalid(code, field, message, fix):
        raise AppError(code, message, details={"category": "result_semantics", "errors": [{
            "loc": [field], "code": code, "message": message, "fix": fix}]})

    acquisition = knowledge.acquisition()
    if answer_basis not in {"general", "case_metadata", "case_material"}:
        invalid("invalid_tool_arguments", "answer_basis", "Unknown answer_basis",
                "Choose general, case_metadata or case_material to match the answer's actual basis.")
    if any(proposal_material_ids(p) for p in proposals):
        if answer_basis != "case_material":
            invalid("invalid_answer_basis", "answer_basis", "Material-backed artifacts require case_material answer_basis",
                    "Use case_material for material-backed Proposals; retain their validated InfoUnit associations.")
    if answer_basis == "case_material":
        investigated = acquisition["has_material"] or acquisition["has_read_result"] or acquisition["read_failed"]
        if case.info_units and not investigated and not read_budget_exhausted:
            invalid("initial_information_required", "answer_basis", "Read relevant case material before a case judgment",
                    "Actually read relevant material before a case judgment; use general or case_metadata only for answers based on those sources.")
        if completion_status == "completed" and not acquisition["has_material"]:
            invalid("evidence_not_obtained", "completion_status", "Empty/error reads do not establish case facts; explain the unresolved condition",
                    "Obtain material needed to complete the Goal, or use evidence_insufficient and describe the missing critical evidence in open_questions.")
    if completion_status == "evidence_insufficient" and not open_questions:
        invalid("missing_evidence_limitation", "open_questions", "Describe the unresolved condition in open_questions",
                "Add the missing critical evidence or unresolved condition that directly prevents completing this Goal to open_questions; do not hide it or change status just to pass validation.")
    if completion_status == "budget_exhausted" and not budget_exhausted:
        invalid("budget_available", "completion_status", "Use an accurate completion_status; execution budget remains available",
                "Budget remains: choose completed if the Goal is answered, evidence_insufficient if critical evidence is missing, or unsupported if the Goal exceeds system capabilities.")
    result = artifacts(case, seen, context, goal, proposals, open_questions, completion_status)
    return result, _validate_summary(answer, proposals)


PROPOSAL_TOOLS = {function.__name__: function for function in (
    propose_hypothesis, propose_evidence_link, propose_evidence_gap, propose_hypothesis_status,
    propose_evidence_gap_update)}
