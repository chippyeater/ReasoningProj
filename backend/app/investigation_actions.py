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
            "proposal": proposal.model_dump(mode="json")}


def propose_hypothesis(case, seen, context, proposals, *, content, rationale, info_unit_ids, evidence_refs):
    """Propose a testable explanation after checking evidence; pending user approval, never verified by default."""
    return _propose(case, seen, context, proposals, "hypothesis", content=content, rationale=rationale,
                    info_unit_ids=info_unit_ids, evidence_refs=evidence_refs)


def propose_evidence_link(case, seen, context, proposals, *, hypothesis_id, info_unit_id, meaning, rationale, evidence_refs):
    """Propose support/oppose/verify between a retrieved InfoUnit and an existing context Hypothesis."""
    return _propose(case, seen, context, proposals, "evidence_link", hypothesis_ids=[hypothesis_id],
                    info_unit_ids=[info_unit_id], meaning=meaning, rationale=rationale, evidence_refs=evidence_refs)


def propose_evidence_gap(case, seen, context, proposals, *, content, hypothesis_ids, hypothesis_proposal_ids,
                         rationale, info_unit_ids, evidence_refs):
    """Propose a discriminating unresolved question, linked to existing Hypotheses or this run's returned Hypothesis proposal IDs."""
    return _propose(case, seen, context, proposals, "evidence_gap", content=content, hypothesis_ids=hypothesis_ids,
                    hypothesis_proposal_ids=hypothesis_proposal_ids, rationale=rationale,
                    info_unit_ids=info_unit_ids, evidence_refs=evidence_refs)


def propose_hypothesis_status(case, seen, context, proposals, *, hypothesis_id, judgment, rationale, info_unit_ids, evidence_refs):
    """Propose an evidence judgment for an existing Hypothesis; missing support does not establish refutation."""
    return _propose(case, seen, context, proposals, "hypothesis_status", hypothesis_ids=[hypothesis_id],
                    judgment=judgment, rationale=rationale, info_unit_ids=info_unit_ids, evidence_refs=evidence_refs)


def present_findings(case, seen, context, goal, proposals, previous_findings, previous_questions, *, findings, open_questions, completion_status):
    """Publish bounded, cited findings for the existing source UI; append distinct findings, never create canvas objects."""
    if not isinstance(findings, list) or len(findings) > 4:
        raise AppError("invalid_findings", "Submit at most four findings per call")
    merged = [*previous_findings]
    for finding in findings:
        if finding not in merged:
            merged.append(finding)
    if not isinstance(open_questions, list) or any(not isinstance(q, str) for q in open_questions):
        raise AppError("invalid_findings", "open_questions must be a list of strings")
    questions = list(dict.fromkeys([*previous_questions, *open_questions]))
    return artifacts(case, seen, context, goal, proposals, merged, questions, completion_status)


PROPOSAL_TOOLS = {function.__name__: function for function in (
    propose_hypothesis, propose_evidence_link, propose_evidence_gap, propose_hypothesis_status)}
