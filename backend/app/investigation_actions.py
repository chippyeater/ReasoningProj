"""Validated investigation artifacts. These tools never write the Issue workspace."""
import json
from uuid import uuid4

from pydantic import TypeAdapter

from app.errors import AppError
from app.reasoning_models import ReasoningProposal


def artifacts(case, seen, context, goal, proposals, findings, questions, status):
    """Reuse citation/kind validation; no final model JSON is involved."""
    from app.investigation_agent import Finding, _validate_result
    findings = [f.model_dump(mode="json") for f in TypeAdapter(list[Finding]).validate_python(findings, strict=True)]
    units, relations, refs = [], [], []
    for item in [*findings, *(p.model_dump(mode="json") for p in proposals)]:
        for uid in item["info_unit_ids"]:
            if uid not in units:
                units.append(uid)
        for rid in item.get("relation_ids", []):
            if rid not in relations:
                relations.append(rid)
        for ref in item["evidence_refs"]:
            if ref not in refs:
                refs.append(ref)
    return _validate_result(json.dumps({
        "goal": goal, "findings": findings, "relevant_info_unit_ids": units,
        "relevant_relation_ids": relations, "evidence_refs": refs, "open_questions": questions,
        "completion_status": status, "proposals": [p.model_dump(mode="json") for p in proposals],
    }, ensure_ascii=False), goal, case, seen, context)


def _propose(case, seen, context, proposals, kind, **values):
    if len(proposals) >= 6:
        raise AppError("proposal_limit", "At most six pending proposals per run")
    proposal = ReasoningProposal.model_validate({
        "kind": kind, "content": None, "hypothesis_ids": [], "meaning": None, "judgment": None,
        **values, "proposal_id": f"proposal-{uuid4().hex}",
    })
    # No append until every citation and current-run dependency has passed.
    artifacts(case, seen, context, "validate pending suggestion", [*proposals, proposal], [], [], "evidence_insufficient")
    proposals.append(proposal)
    return {"status": "ok", "proposal_id": proposal.proposal_id, "pending": True,
            "evidence_status": "traceable_source" if proposal.evidence_refs else "not_directly_evidenced",
            "proposal": proposal.model_dump(mode="json")}


def propose_hypothesis(case, seen, context, proposals, *, content, rationale, info_unit_ids, evidence_refs):
    """Propose a testable explanation, pending user approval, never verified by default. Exploratory hypotheses may use empty info_unit_ids/evidence_refs; explain uncertainty in rationale. Cite validated initial source_refs or get_evidence results when available; source location alone does not prove the hypothesis."""
    return _propose(case, seen, context, proposals, "hypothesis", content=content, rationale=rationale,
                    info_unit_ids=info_unit_ids, evidence_refs=evidence_refs)


def propose_evidence_link(case, seen, context, proposals, *, hypothesis_id, info_unit_id, meaning, rationale, evidence_refs,
                          hypothesis_proposal_ids=None):
    """Propose support/oppose/verify from a provided InfoUnit with a verified source_refs or get_evidence citation to exactly one Hypothesis. Use hypothesis_id for an existing context Hypothesis, or null plus one actually returned same-run hypothesis_proposal_ids entry for a new pending Hypothesis. Never both. User must adopt that Hypothesis first, then separately adopt this Link; nothing is automatically accepted."""
    return _propose(case, seen, context, proposals, "evidence_link", hypothesis_ids=[] if hypothesis_id is None else [hypothesis_id],
                    hypothesis_proposal_ids=[] if hypothesis_proposal_ids is None else hypothesis_proposal_ids,
                    info_unit_ids=[info_unit_id], meaning=meaning, rationale=rationale, evidence_refs=evidence_refs)


def propose_evidence_gap(case, seen, context, proposals, *, content, hypothesis_ids, hypothesis_proposal_ids,
                         rationale, info_unit_ids, evidence_refs):
    """Propose a discriminating unresolved question linked to existing Hypotheses or this run's returned Hypothesis proposal IDs. May use empty info_unit_ids/evidence_refs when no direct evidence exists; describe what is missing, not a proven fact."""
    return _propose(case, seen, context, proposals, "evidence_gap", content=content, hypothesis_ids=hypothesis_ids,
                    hypothesis_proposal_ids=hypothesis_proposal_ids, rationale=rationale,
                    info_unit_ids=info_unit_ids, evidence_refs=evidence_refs)


def propose_hypothesis_status(case, seen, context, proposals, *, hypothesis_id, judgment, rationale, info_unit_ids, evidence_refs):
    """Propose an evidence judgment for an existing Hypothesis. Requires at least one provided InfoUnit and verified citation, from initial source_refs or get_evidence. Missing support does not establish refutation."""
    return _propose(case, seen, context, proposals, "hypothesis_status", hypothesis_ids=[hypothesis_id],
                    judgment=judgment, rationale=rationale, info_unit_ids=info_unit_ids, evidence_refs=evidence_refs)


def finish_investigation(case, seen, context, goal, proposals, *, answer, findings,
                         open_questions, completion_status, answer_basis, knowledge,
                         budget_exhausted=False, read_budget_exhausted=False):
    """Deliver a concise answer to the Goal, preserving actual pending Proposals. answer_basis distinguishes general knowledge/capabilities, case metadata, and case material. Case judgments require obtained material; failed/empty reads can justify an explicit evidence limitation. Findings are optional; when supplied use obtained IDs and source pointers. Report unresolved conditions in open_questions and use evidence_insufficient when they prevent answering the Goal. No fixed retrieval depth or Proposal count."""
    from app.investigation_agent import _validate_summary
    acquisition = knowledge.acquisition()
    if answer_basis not in {"general", "case_metadata", "case_material"}:
        raise AppError("invalid_tool_arguments", "Unknown answer_basis")
    if findings or any(p.info_unit_ids or p.evidence_refs for p in proposals):
        if answer_basis != "case_material":
            raise AppError("invalid_answer_basis", "Material-backed artifacts require case_material answer_basis")
    if answer_basis == "case_material":
        investigated = acquisition["has_material"] or acquisition["has_read_result"] or acquisition["read_failed"]
        if case.info_units and not investigated and not read_budget_exhausted:
            raise AppError("initial_information_required", "Read relevant case material before a case judgment; directory titles are discovery metadata")
        if completion_status == "completed" and not acquisition["has_material"]:
            raise AppError("evidence_not_obtained", "Empty/error reads do not establish case facts; explain the unresolved condition")
    if completion_status == "evidence_insufficient" and not open_questions:
        raise AppError("missing_evidence_limitation", "Describe the unresolved condition in open_questions")
    if completion_status == "completed" and open_questions:
        raise AppError("unresolved_goal", "Unresolved Goal conditions require evidence_insufficient")
    if completion_status == "budget_exhausted" and not budget_exhausted:
        raise AppError("budget_available", "Use an accurate completion_status; execution budget remains available")
    result = artifacts(case, seen, context, goal, proposals, findings, open_questions, completion_status)
    for index, finding in enumerate(result.findings):
        if not finding.evidence_refs and any(uid == ref[0] for uid in finding.info_unit_ids for ref in seen["evidence"]):
            raise AppError("finding_sources_missing", "Include obtained source pointers with factual Findings", details={
                "errors": [{"loc": ["findings", index, "evidence_refs"], "code": "finding_sources_missing"}]})
    return result, _validate_summary(answer, proposals)


PROPOSAL_TOOLS = {function.__name__: function for function in (
    propose_hypothesis, propose_evidence_link, propose_evidence_gap, propose_hypothesis_status)}
