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
                         question_updates, completion_status, knowledge, budget_exhausted=False):
    """Finish with answer, cited findings and changed Questions; question_updates=[] reuses validated working state. Failures list independent field errors with fix hints and report whether Question updates were retained; failed answer/findings are never accepted. Plans use actual same-run proposal_id, matching kind, recorded Question scope and material/dependency links, not identical prose; preserve validated bindings. Q1 retains the full goal; unresolved aspects must be specifically blocked. Resolve planned actions; Runtime derives open_questions/Proposals. No minimum read/Proposal count or extra summary call."""
    from pydantic import ValidationError
    from app.investigation_agent import Finding, _validate_summary
    from app.investigation_state import validation_issues

    draft, errors = questions.review_updates(question_updates, case, seen, context, proposals, knowledge)
    question_errors = bool(errors)
    # A diagnostic draft can contain invalid bindings. Never accept that batch.
    for issue in errors:
        issue.setdefault("error_code", issue["code"] if issue["code"] in {
            "invalid_plan_reference", "unresolved_plan", "goal_scope_changed", "invalid_questions",
            "question_limit", "invalid_investigation_reference"} else "invalid_tool_arguments")

    def error(loc, code, message, fix, *, error_code="investigation_incomplete"):
        errors.append({"loc": loc, "code": code, "message": message, "fix": fix,
                       "error_code": error_code})

    for q in draft.items.values():
        if q.status in {"pending", "in_progress"}:
            error(["questions", q.id], "question_unresolved", q.question,
                  "Answer this Question with an obtained basis, or block it with the specific missing condition/kind")
        for plan in q.planned_actions:
            if not plan.proposal_id and not plan.omission_reason:
                error(["questions", q.id, "planned_actions", plan.id], "unresolved_plan", plan.description,
                      "Use its actual same-run Proposal ID or record omission_reason; retain registered action IDs")
    blocked = [q for q in draft.items.values() if q.status == "blocked"]
    if draft.items["Q1"].status == "answered" and any(q.id != "Q1" and q.status != "answered" for q in draft.items.values()):
        question_errors = True
        error(["questions", "Q1", "status"], "goal_scope_incomplete", "Q1 cannot be answered while a requested aspect is unresolved",
              "Keep Q1 blocked and report the unresolved aspect")
    if completion_status == "evidence_insufficient" and not blocked:
        error(["completion_status"], "insufficiency_without_unresolved_question", "All-answered is inconsistent with evidence insufficiency",
              "Use completed for resolved work; otherwise identify and block the missing condition")
    if completion_status == "completed" and blocked:
        error(["completion_status"], "blocked_questions", "Blocked Questions require an incomplete completion_status",
              "Use evidence_insufficient, unsupported or budget_exhausted as justified by the actual state")
    if completion_status == "budget_exhausted" and not budget_exhausted:
        error(["completion_status"], "budget_available", "Execution budget is still available", "Continue useful work or choose an accurate completion_status")
    for q in blocked:
        if q.block_kind == "budget_exhausted" and not budget_exhausted:
            question_errors = True
            error(["questions", q.id, "block_kind"], "budget_available", "Cannot claim budget blocking while budget is available",
                  "Continue useful work or describe the actual missing evidence/tool failure")
        if completion_status == "unsupported" and q.block_kind != "unsupported":
            error(["questions", q.id, "block_kind"], "unsupported_mismatch", "Unsupported completion requires unsupported Questions",
                  "Choose the status describing the actual restriction")
    if completion_status == "completed" and not findings:
        error(["findings"], "invalid_findings", "Completed investigation requires a grounded finding",
              "Include a finding with its obtained material and source pointers", error_code="invalid_findings")

    # Validate independent finding rows even when a different row is malformed.
    parsed_findings = []
    if not isinstance(findings, list) or len(findings) > 12:
        error(["findings"], "invalid_findings", "findings must be a list of at most twelve findings",
              "Use [] or a bounded list of grounded findings", error_code="invalid_findings")
    if isinstance(findings, list):
        for index, row in enumerate(findings):
            try:
                parsed_findings.append((index, Finding.model_validate(row, strict=True)))
            except ValidationError as exc:
                errors.extend(validation_issues(exc, ["findings", index]))
    result = None
    try:
        result = artifacts(case, seen, context, goal, proposals,
            [f.model_dump(mode="json") for _, f in parsed_findings], draft.open_questions(), completion_status)
    except (AppError, ValidationError) as exc:
        rows = validation_issues(exc, fix="Use obtained current-case IDs and verified source pointers")
        for issue in rows:
            path = issue["loc"]
            if path[:1] == ["findings"] and len(path) > 1 and isinstance(path[1], int):
                path[1] = parsed_findings[path[1]][0]
            issue.setdefault("error_code", exc.code if isinstance(exc, AppError) else "invalid_tool_arguments")
        errors.extend(rows)
    delivered_units = {uid for _, f in parsed_findings for uid in f.info_unit_ids}
    delivered_units.update(uid for p in proposals for uid in p.info_unit_ids)
    delivered_refs = {(r.info_unit_id, r.source_ref_index) for item in [*(f for _, f in parsed_findings), *proposals] for r in item.evidence_refs}
    for q in draft.items.values():
        if q.status == "answered" and (not set(q.related_info_unit_ids) & delivered_units
                or not {(r.info_unit_id, r.source_ref_index) for r in q.evidence_refs} <= delivered_refs):
            error(["questions", q.id], "question_basis_not_delivered", "Answered Question basis must appear in final findings or Proposals",
                  "Include the Question's obtained materials and every cited SourceRef in final findings")
    for index, finding in parsed_findings:
        if finding.info_unit_ids and not finding.evidence_refs and any(uid == ref[0] for uid in finding.info_unit_ids for ref in seen["evidence"]):
            error(["findings", index, "evidence_refs"], "finding_sources_missing", "Include obtained source pointers with factual findings",
                  "Copy the verified SourceRef pointers for these materials from knowledge_state")
    text = None
    try:
        text = _validate_summary(answer, proposals)
    except AppError as exc:
        for issue in validation_issues(exc, fix="Write concise prose within 240 nonblank characters without repeating Proposal fields"):
            issue["error_code"] = exc.code
            errors.append(issue)

    # Reuse a completely validated Question batch as working memory even if
    # delivery fails elsewhere. Findings/answer/completion are never accepted.
    if not question_errors and question_updates:
        questions.items, questions.activities = draft.items, draft.activities
    if errors:
        indices = {q["id"]: i for i, q in enumerate(question_updates) if isinstance(q, dict) and isinstance(q.get("id"), str)} if isinstance(question_updates, list) else {}

        def references(item, field):
            values = getattr(item, field, [])
            return [(r.info_unit_id, r.source_ref_index) for r in values] if field == "evidence_refs" else values

        for issue in errors:
            path = issue["loc"]
            field = {"relevant_info_unit_ids": "info_unit_ids", "relevant_relation_ids": "relation_ids",
                     "evidence_refs": "evidence_refs"}.get(path[0] if path else None)
            if field and len(path) > 1 and isinstance(path[1], int):
                # artifacts() derives unions; translate their indices back to
                # the actual tool arguments, including every repeated use.
                union = []
                for item in [*(f for _, f in parsed_findings), *proposals]:
                    for value in references(item, field):
                        if value not in union:
                            union.append(value)
                if path[1] < len(union):
                    locations = [["findings", index, field, offset] for index, f in parsed_findings
                                 for offset, value in enumerate(references(f, field)) if value == union[path[1]]]
                    if locations:
                        issue["argument_loc"] = locations[0]
                        if len(locations) > 1:
                            issue["argument_locs"] = locations
            if path[:1] == ["questions"]:
                qid = path[1] if len(path) > 1 else None
                mapped = (["question_updates", *path[1:]] if isinstance(qid, int) else
                          ["question_updates", indices[qid], *path[2:]] if qid in indices else ["question_updates"])
                if len(mapped) > 3 and mapped[2] == "planned_actions" and isinstance(mapped[3], str):
                    plans = question_updates[mapped[1]].get("planned_actions", [])
                    mapped[3] = next((i for i, p in enumerate(plans) if isinstance(p, dict) and p.get("id") == mapped[3]), mapped[3])
                issue["argument_loc"] = mapped
        raise AppError(errors[0].get("error_code", "investigation_incomplete"), "Finish rejected; address all listed issues before retrying",
            details={"category": errors[0].get("category", "completion"), "errors": errors,
                     "question_updates_applied": not question_errors,
                     "updated_question_ids": list(indices) if not question_errors else [],
                     "question_recovery": "Validated Question updates are working memory only. Use question_updates=[] to reuse them; send only changed Questions."
                        if not question_errors else "Question update batch was not accepted. Restore registered IDs and fix all Question errors; other diagnostics use the tentative draft."})
    return result, draft, text


PROPOSAL_TOOLS = {function.__name__: function for function in (
    propose_hypothesis, propose_evidence_link, propose_evidence_gap, propose_hypothesis_status)}
