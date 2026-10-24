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
    """Propose a testable explanation, pending user approval, never verified by default. Exploratory hypotheses may use empty info_unit_ids/evidence_refs; explain uncertainty in rationale. Cite validated initial evidence_context or get_evidence results when available; source location alone does not prove the hypothesis."""
    return _propose(case, seen, context, proposals, "hypothesis", content=content, rationale=rationale,
                    info_unit_ids=info_unit_ids, evidence_refs=evidence_refs)


def propose_evidence_link(case, seen, context, proposals, *, hypothesis_id, info_unit_id, meaning, rationale, evidence_refs,
                          hypothesis_proposal_ids=None):
    """Propose support/oppose/verify from a provided InfoUnit with a verified evidence_context or get_evidence citation to exactly one Hypothesis. Use hypothesis_id for an existing context Hypothesis, or null plus one actually returned same-run hypothesis_proposal_ids entry for a new pending Hypothesis. Never both. User must adopt that Hypothesis first, then separately adopt this Link; nothing is automatically accepted."""
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
    """Propose an evidence judgment for an existing Hypothesis. Requires at least one provided InfoUnit and verified citation, from initial evidence_context or get_evidence. Missing support does not establish refutation."""
    return _propose(case, seen, context, proposals, "hypothesis_status", hypothesis_ids=[hypothesis_id],
                    judgment=judgment, rationale=rationale, info_unit_ids=info_unit_ids, evidence_refs=evidence_refs)


def finish_investigation(case, seen, context, goal, proposals, questions, *, answer, findings,
                         question_updates, completion_status, knowledge, budget_exhausted=False, read_budget_exhausted=False):
    """Finish with answer, cited findings and final Question updates; no extra summary call. Q1 retains the full goal; track independent requested aspects as Questions. Unanswered aspects must be specifically blocked, never answered merely because evidence conflicts. Before claiming insufficiency inspect useful investigation_opportunities or record each skipped_leads {lead_id, reason} explaining why it cannot resolve the missing condition. This is a model relevance judgment, not proof. Resolve planned actions; Runtime derives open_questions/Proposals. No read quota or required Proposal count."""
    from copy import deepcopy
    from app.investigation_agent import _validate_summary
    if not isinstance(question_updates, list) or len(question_updates) > 20:
        raise AppError("invalid_questions", "question_updates must be a list of at most twenty Questions")
    draft = deepcopy(questions)
    if question_updates:
        draft.update(question_updates, case, seen, context, proposals)
    pending = []
    for q in draft.items.values():
        if q.status in {"pending", "in_progress"}:
            pending.append({"loc": ["questions", q.id], "code": "question_unresolved", "message": q.question})
        for plan in q.planned_actions:
            if not plan.proposal_id and not plan.omission_reason:
                pending.append({"loc": ["questions", q.id, "planned_actions", plan.id],
                    "code": "unresolved_plan", "message": plan.description})
    if pending:
        raise AppError("investigation_incomplete", "Resolve the listed Questions/planned actions before finishing",
                       details={"category": "completion", "errors": pending})
    blocked = [q for q in draft.items.values() if q.status == "blocked"]
    if draft.items["Q1"].status == "answered" and blocked:
        raise AppError("investigation_incomplete", "Q1 covers the whole goal and cannot be answered while a requested aspect remains blocked",
            details={"category": "completion", "errors": [{"loc": ["questions", "Q1", "status"], "code": "goal_scope_incomplete"}]})
    if completion_status == "evidence_insufficient" and not blocked:
        raise AppError("investigation_incomplete", "Evidence insufficiency requires an explicitly unresolved Question and missing condition; all-answered is inconsistent",
            details={"category": "completion", "errors": [{"loc": ["completion_status"], "code": "insufficiency_without_unresolved_question"}]})
    if completion_status == "completed" and blocked:
        raise AppError("investigation_incomplete", "Blocked Questions require an incomplete completion_status")
    if completion_status == "budget_exhausted" and not budget_exhausted:
        raise AppError("investigation_incomplete", "Execution budget is still available")
    if any(q.block_kind == "budget_exhausted" for q in blocked) and not budget_exhausted:
        raise AppError("investigation_incomplete", "Cannot claim budget blocking while budget is available")
    if completion_status == "unsupported" and any(q.block_kind != "unsupported" for q in blocked):
        raise AppError("investigation_incomplete", "Unsupported completion requires unsupported Questions")
    if completion_status == "completed" and not findings:
        raise AppError("invalid_findings", "Completed investigation requires a grounded finding")
    opportunities = knowledge.available_work(case, draft)
    known_leads = {prefix + u.id for u in case.info_units for prefix in ("unit:", "source:")}
    known_leads.update(f"query:{i}" for i, a in enumerate(knowledge.activities) if a["has_more"])
    review_errors = []
    for q in draft.items.values():
        assessed = {assessment.lead_id for assessment in q.skipped_leads}
        if not assessed <= known_leads:
            raise AppError("invalid_investigation_reference", "Skipped leads must reference current-case material or actual truncated queries")
        if q.status != "blocked" or q.block_kind == "unsupported":
            continue
        for lead in opportunities[q.id]["items"]:
            if lead["lead_id"] in assessed:
                continue
            if read_budget_exhausted and q.block_kind == "budget_exhausted":
                continue
            review_errors.append({"loc": ["questions", q.id, "skipped_leads"],
                "code": "uninvestigated_lead", "opportunity": lead,
                "message": "Investigate this lead or explain why it cannot resolve the missing condition; if reads are exhausted report budget blocking, not missing evidence"})
    if review_errors:
        raise AppError("investigation_incomplete", "Relevant material may still be available; inspect or explicitly assess remaining work",
                       details={"category": "completion", "errors": review_errors})
    result = artifacts(case, seen, context, goal, proposals, findings, draft.open_questions(), completion_status)
    delivered_refs = {(r.info_unit_id, r.source_ref_index) for r in result.evidence_refs}
    for q in draft.items.values():
        if q.status == "answered" and (not set(q.related_info_unit_ids) & set(result.relevant_info_unit_ids)
                or not {(r.info_unit_id, r.source_ref_index) for r in q.evidence_refs} <= delivered_refs):
            raise AppError("investigation_incomplete", "Answered Question basis must appear in final findings or Proposals",
                details={"category": "completion", "errors": [{"loc": ["questions", q.id], "code": "question_basis_not_delivered"}]})
    if any(f.info_unit_ids and not f.evidence_refs and any(uid == ref[0] for uid in f.info_unit_ids for ref in seen["evidence"])
           for f in result.findings):
        raise AppError("investigation_incomplete", "Include obtained source pointers with factual findings")
    text = _validate_summary(answer, proposals)
    return result, draft, text


PROPOSAL_TOOLS = {function.__name__: function for function in (
    propose_hypothesis, propose_evidence_link, propose_evidence_gap, propose_hypothesis_status)}
