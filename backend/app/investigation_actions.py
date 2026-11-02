"""Validated investigation artifacts. These tools never write the Issue workspace."""
import json
import re
from uuid import uuid4

from pydantic import TypeAdapter

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
        text = _normalized(content)
        text = re.sub(r"^(确认|核实|核对|查明|confirm\s+|verify\s+)", "", text)
        # A trailing explanation/evidence request does not rename a condition.
        parts = re.split(r"[:：]", text, maxsplit=1)
        if len(parts[0]) >= 6 and not re.search(r"假设|解释|hypothesis|explanation", parts[0]):
            text = parts[0]
        if proposal.kind == "evidence_gap":
            # Resolve a leading short label(alias), retaining the named subject.
            # Do not use a similarity score: nearby wording can describe a
            # different party, date or condition and must remain independent.
            text = re.sub(r"^[^()]{1,4}\(([^()\d]{1,8})\)", r"\1", text)
            text = re.sub(r"\(([^)]*)\)",
                          lambda match: match[1] if re.search(r"\d", match[1]) else "", text)
        return re.sub(r"[\W_]", "", text)

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


def artifacts(case, seen, context, goal, proposals, findings, questions, status):
    """Reuse citation/kind validation; no final model JSON is involved."""
    from app.investigation_agent import Finding, _validate_result
    findings = [f.model_dump(mode="json") for f in TypeAdapter(list[Finding]).validate_python(findings, strict=True)]
    units, relations, refs = [], [], []
    for item in [*findings, *(p.model_dump(mode="json") for p in proposals)]:
        material_ids = (item["basis_info_unit_ids"] if item.get("kind") == "evidence_gap" else
                        item["addressing_info_unit_ids"] if item.get("kind") == "evidence_gap_update" else item["info_unit_ids"])
        for uid in material_ids:
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
    proposal = PROPOSAL_ADAPTER.validate_python({
        "kind": kind, "content": None, "hypothesis_ids": [], "meaning": None, "judgment": None,
        **values, "proposal_id": f"proposal-{uuid4().hex}",
    })
    # No append until every citation and current-run dependency has passed.
    artifacts(case, seen, context, "validate pending suggestion", [*proposals, proposal], [], [], "evidence_insufficient")
    _check_duplicate(proposal, context, proposals, case)
    proposals.append(proposal)
    return {"status": "ok", "proposal_id": proposal.proposal_id, "pending": True,
            "evidence_status": "traceable_source" if proposal.evidence_refs else "not_directly_evidenced",
            "proposal": proposal.model_dump(mode="json")}


def propose_hypothesis(case, seen, context, proposals, *, content, rationale, info_unit_ids, evidence_refs):
    """Propose a testable explanation, pending user approval, never verified by default. Before calling compare reasoning and every successful same-run Proposal: reuse synonymous Hypotheses; competing explanations must state substantively different conditions or distinguishing evidence. Obtained InfoUnit content can support exploratory reasoning without get_evidence; evidence_refs may be empty. If no direct material exists info_unit_ids may also be empty; explain uncertainty in rationale. Cite validated initial source_refs or get_evidence results when available; source location alone does not prove the hypothesis."""
    return _propose(case, seen, context, proposals, "hypothesis", content=content, rationale=rationale,
                    info_unit_ids=info_unit_ids, evidence_refs=evidence_refs)


def propose_evidence_link(case, seen, context, proposals, *, hypothesis_id, info_unit_id, meaning, rationale, evidence_refs,
                          hypothesis_proposal_ids=None):
    """Propose support/oppose/related (related means relevant to the Hypothesis without clearly supporting or opposing it) from a provided InfoUnit with a verified source_refs or get_evidence citation to exactly one Hypothesis. Use hypothesis_id for an existing context Hypothesis, or null plus one actually returned same-run hypothesis_proposal_ids entry for a new pending Hypothesis. Never both. User must adopt that Hypothesis first, then separately adopt this Link; nothing is automatically accepted."""
    return _propose(case, seen, context, proposals, "evidence_link", hypothesis_ids=[] if hypothesis_id is None else [hypothesis_id],
                    hypothesis_proposal_ids=[] if hypothesis_proposal_ids is None else hypothesis_proposal_ids,
                    info_unit_ids=[info_unit_id], meaning=meaning, rationale=rationale, evidence_refs=evidence_refs)


def propose_evidence_gap(case, seen, context, proposals, *, content, hypothesis_ids, hypothesis_proposal_ids,
                         rationale, basis_info_unit_ids, evidence_refs):
    """Propose one specific, independently verifiable missing fact or condition. hypothesis_targets contains hypothesis_ids and hypothesis_proposal_ids: at least one array MUST be nonempty, with at most five targets total. Use actual current-Issue Hypothesis IDs or first submit propose_hypothesis successfully and use its returned proposal_id in hypothesis_proposal_ids; never guess IDs or send two empty arrays. Compare reasoning and all successful same-run Proposals before calling: reuse existing equivalent conditions, and create a shared Gap once, after obtaining IDs of all relevant competing Hypotheses. State what must be confirmed and why it affects the Hypotheses, with a concrete evidence action or material type. Split independently useful conditions into separate Gaps; several materials checking the same condition can share one Gap. Do not bundle a new lease, payment records, move-out proof and expense receipts into a broad request. Obtained InfoUnit content is sufficient for an exploratory Gap without get_evidence; evidence_refs and basis_info_unit_ids may be empty when no direct material exists. basis_info_unit_ids records existing forming material, not later addressing material; a new Gap starts with no addressing material. Describe uncertainty, not a proven fact."""
    return _propose(case, seen, context, proposals, "evidence_gap", content=content, hypothesis_ids=hypothesis_ids,
                    hypothesis_proposal_ids=hypothesis_proposal_ids, rationale=rationale,
                    basis_info_unit_ids=basis_info_unit_ids, evidence_refs=evidence_refs)


def propose_hypothesis_status(case, seen, context, proposals, *, hypothesis_id, judgment, rationale, info_unit_ids, evidence_refs):
    """Propose an evidence judgment for an existing Hypothesis. Requires at least one provided InfoUnit and verified citation, from initial source_refs or get_evidence. Missing support does not establish refutation."""
    return _propose(case, seen, context, proposals, "hypothesis_status", hypothesis_ids=[hypothesis_id],
                    judgment=judgment, rationale=rationale, info_unit_ids=info_unit_ids, evidence_refs=evidence_refs)


def propose_evidence_gap_update(case, seen, context, proposals, *, gap_id, addressing_info_unit_ids,
                                status, rationale, evidence_refs):
    """Propose addressing material for an existing Gap in the current Issue. Use its actual gap_id, obtained InfoUnits and verified source citations. Compare new material with the Gap and its competing Hypotheses; partial responses keep status=open and explain what remains missing. Use resolved only when the missing condition is sufficiently addressed. Adoption preserves the Gap ID, content, basis and Hypothesis associations, appends unique addressing IDs and checks its captured revision. Prefer this over recreating the same Gap; user approval is always required."""
    return _propose(case, seen, context, proposals, "evidence_gap_update", gap_id=gap_id,
                    addressing_info_unit_ids=addressing_info_unit_ids, status=status,
                    rationale=rationale, evidence_refs=evidence_refs)


def finish_investigation(case, seen, context, goal, proposals, *, answer, findings,
                         open_questions, completion_status, answer_basis, knowledge,
                         budget_exhausted=False, read_budget_exhausted=False):
    """Deliver a concise answer to the Goal, preserving actual pending Proposals. answer_basis distinguishes general knowledge/capabilities, case metadata, and case material. Case judgments require obtained material; failed/empty reads can justify an explicit evidence limitation. Findings are optional; when supplied use obtained IDs and source pointers. Report unresolved conditions in open_questions and use evidence_insufficient when they prevent answering the Goal. No fixed retrieval depth or Proposal count."""
    from app.investigation_agent import _validate_summary
    acquisition = knowledge.acquisition()
    if answer_basis not in {"general", "case_metadata", "case_material"}:
        raise AppError("invalid_tool_arguments", "Unknown answer_basis")
    if findings or any(proposal_material_ids(p) or p.evidence_refs for p in proposals):
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
    units = {unit.id: unit for unit in case.info_units}
    errors = []
    for index, finding in enumerate(result.findings):
        if finding.evidence_refs:
            continue
        for uid in finding.info_unit_ids:
            unit = units[uid]
            indices = sorted(source_index for obtained_uid, source_index in seen["evidence"]
                             if obtained_uid == uid and 0 <= source_index < len(unit.source_refs))
            if indices:
                errors.append({"loc": ["findings", index, "evidence_refs"], "code": "finding_sources_missing",
                    "finding_index": index, "info_unit_id": uid, "info_unit_title": unit.title,
                    "obtained_source_ref_indices": indices,
                    "available_evidence_refs": [{"info_unit_id": uid, "source_ref_index": i} for i in indices],
                    "message": f"Finding {index} omits already obtained SourceRefs for {uid} ({unit.title}): {indices}",
                    "fix": "Add the available obtained source pointers to this Finding if they support it; otherwise remove the unsupported Finding or put unresolved conditions in open_questions."})
    if errors:
        raise AppError("finding_sources_missing", "Include obtained source pointers with factual Findings",
                       details={"category": "reference", "errors": errors})
    return result, _validate_summary(answer, proposals)


PROPOSAL_TOOLS = {function.__name__: function for function in (
    propose_hypothesis, propose_evidence_link, propose_evidence_gap, propose_hypothesis_status,
    propose_evidence_gap_update)}
