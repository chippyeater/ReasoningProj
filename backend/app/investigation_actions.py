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
    """Submit one evidence-testable proposition in content, with only its own basis/uncertainty in rationale. Use obtained InfoUnit IDs; exploratory evidence_refs may be empty, and info_unit_ids may be empty if no direct material exists. Submission creates a pending suggestion, not a verified fact."""
    return _propose(case, seen, context, proposals, "hypothesis", content=content, rationale=rationale,
                    info_unit_ids=info_unit_ids, evidence_refs=evidence_refs)


def propose_evidence_link(case, seen, context, proposals, *, hypothesis_id, info_unit_id, meaning, rationale, evidence_refs,
                          hypothesis_proposal_ids=None):
    """Link one obtained InfoUnit and source citation to one Hypothesis. Use a formal hypothesis_id, or null plus one returned hypothesis_proposal_ids entry, never both. For a pending parent, the user must adopt the Hypothesis before separately adopting this Link."""
    return _propose(case, seen, context, proposals, "evidence_link", hypothesis_ids=[] if hypothesis_id is None else [hypothesis_id],
                    hypothesis_proposal_ids=[] if hypothesis_proposal_ids is None else hypothesis_proposal_ids,
                    info_unit_ids=[info_unit_id], meaning=meaning, rationale=rationale, evidence_refs=evidence_refs)


def propose_evidence_gap(case, seen, context, proposals, *, content, hypothesis_ids, hypothesis_proposal_ids,
                         rationale, basis_info_unit_ids, evidence_refs):
    """Submit one unresolved factual question. hypothesis_targets must contain 1–5 actual formal Hypothesis IDs and/or successfully returned Hypothesis proposal IDs. One or several propositions may share the question. basis_info_unit_ids and evidence_refs may be empty; a new Gap has no addressing material."""
    return _propose(case, seen, context, proposals, "evidence_gap", content=content, hypothesis_ids=hypothesis_ids,
                    hypothesis_proposal_ids=hypothesis_proposal_ids, rationale=rationale,
                    basis_info_unit_ids=basis_info_unit_ids, evidence_refs=evidence_refs)


def propose_hypothesis_status(case, seen, context, proposals, *, hypothesis_id, judgment, rationale, info_unit_ids, evidence_refs):
    """Suggest unverified/verified/refuted for an existing hypothesis_id, using obtained InfoUnits and SourceRef citations. Explain how the evidence bears on that proposition; missing support alone does not establish refutation."""
    return _propose(case, seen, context, proposals, "hypothesis_status", hypothesis_ids=[hypothesis_id],
                    judgment=judgment, rationale=rationale, info_unit_ids=info_unit_ids, evidence_refs=evidence_refs)


def propose_evidence_gap_update(case, seen, context, proposals, *, gap_id, addressing_info_unit_ids,
                                status, rationale, evidence_refs):
    """Update an existing gap_id with obtained addressing_info_unit_ids and SourceRef citations. Suggest open or resolved and explain remaining uncertainty. Adoption appends addressing material and preserves the Gap's ID, question, basis and Hypothesis associations."""
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
