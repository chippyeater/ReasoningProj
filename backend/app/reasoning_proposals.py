"""Stage validated Proposals, then atomically commit or undo an Investigation run."""
import hashlib
import json

from app.case_store import load_case_snapshot, read_parsed_document, artifact_path
from app.errors import AppError
from app.issue_workspaces import get_issue_workspaces, persist_issue_workspaces, workspace_transaction
from app.reasoning_models import GapEdit, HypothesisEdit, LinkEdit, ReasoningEdit, ReasoningState, proposal_material_ids, PROPOSAL_ADAPTER
from app.reasoning_service import edit_reasoning
from app.reasoning_models import ReasoningChange, ReasoningRun, read_stored_proposal
from app.schemas import Point


def evidence_hash(case, ids):
    units = {unit.id: unit.model_dump(mode="json") for unit in case.info_units}
    return hashlib.sha256(json.dumps([units.get(uid) for uid in ids], sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def proposal_receipt(case, issue_key, proposals, baseline):
    if issue_key is None:
        raise AppError("invalid_reasoning_proposal", "No Issue is bound to the proposal")
    return {"issue": issue_key, "baseline": baseline.model_dump(mode="json"),
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
        existing = next((link for link in state.links if link.hypothesis_id == hypothesis_ids[0]
                         and link.info_unit_id == proposal.info_unit_ids[0]), None)
        edit.link = LinkEdit(id=existing.id if existing else None, hypothesis_id=hypothesis_ids[0], info_unit_id=proposal.info_unit_ids[0],
                             meaning=proposal.meaning, rationale=proposal.rationale)
    elif proposal.kind == "evidence_gap_update":
        gap = next(g for g in state.gaps if g.id == proposal.gap_id)
        edit.gap = GapEdit(id=gap.id, content=gap.content, hypothesis_ids=list(gap.hypothesis_ids),
                           addressing_info_unit_ids=proposal.addressing_info_unit_ids, status=proposal.status)
    else:
        target = next(h for h in state.hypotheses if h.id == hypothesis_ids[0])
        edit.hypothesis = HypothesisEdit(id=target.id, content=target.content, judgment=proposal.judgment, work_status=target.work_status)
    return edit


def _objects(state):
    return {(section, obj.id): obj for section in ("hypotheses", "gaps", "links") for obj in getattr(state, section)}


def stage_proposals(case, issue, proposals, current, run_id):
    """Reuse real edits on a detached workspace; no item persists or checks siblings' revisions."""
    validate_proposal_dependencies(proposals)
    staged = current.model_copy(deep=True)
    state = staged.workspaces[issue].reasoning
    original = _objects(state.model_copy(deep=True))
    causes = {}
    statuses = {}
    # Allocate formal parent IDs before resolving shared Gap / Link references.
    order = sorted(range(len(proposals)), key=lambda i: proposals[i].kind != "hypothesis")
    for index in order:
        proposal = proposals[index]
        key = proposal_key(run_id, proposals, index)
        if proposal.kind in {"hypothesis_status", "evidence_gap_update"}:
            target = proposal.hypothesis_ids[0] if proposal.kind == "hypothesis_status" else proposal.gap_id
            value = proposal.judgment if proposal.kind == "hypothesis_status" else proposal.status
            if target in statuses and statuses[target] != value:
                raise AppError("conflicting_reasoning_proposals", "Conflicting statuses for the same object", 409,
                    details={"category": "proposal_semantics", "errors": [{"loc": ["status" if proposal.kind == "evidence_gap_update" else "judgment"],
                        "code": "conflicting_reasoning_proposals", "message": "Keep the previously staged status; do not submit a conflicting status in this run"}]})
            statuses[target] = value
        hypothesis_ids = [*proposal.hypothesis_ids, *(state.accepted_proposals[proposal_key(run_id, proposals, i)]
                          for i in dependency_indices(proposal, proposals))]
        if proposal.kind == "evidence_link":
            target = (hypothesis_ids[0], proposal.info_unit_ids[0])
            value = (proposal.meaning, proposal.rationale)
            if target in statuses and statuses[target] != value:
                raise AppError("conflicting_reasoning_proposals", "This evidence link already has a different staged edit", 409)
            statuses[target] = value
        before = _objects(state.model_copy(deep=True))
        try:
            edit = _proposal_edit(proposal, hypothesis_ids, state, issue,
                Point(x=800 + (index % 2) * 440, y=160 + (index // 2) * 220))
            staged, object_id = edit_reasoning(case, edit, proposal_key=key, current=staged, persist=False)
        except StopIteration as exc:
            raise AppError("unknown_reasoning_object", "Proposal target no longer exists", 409) from exc
        state = staged.workspaces[issue].reasoning
        after = _objects(state)
        changed = [oid for oid in before.keys() | after.keys() if before.get(oid) != after.get(oid)]
        if changed:
            state.accepted_proposals[key] = object_id
            for oid in changed:
                causes.setdefault(oid, []).append(key)
    final = _objects(state)
    changes = [ReasoningChange(collection=section, object_id=oid, before=original.get((section, oid)),
                              after=final.get((section, oid)), proposal_keys=causes.get((section, oid), []))
               for section, oid in dict.fromkeys([*original, *final])
               if original.get((section, oid)) != final.get((section, oid))]
    staged.active_issue = current.active_issue
    return staged, changes


@workspace_transaction
def commit_investigation(case, issue, run_id, proposals, receipt, *, validate=None, obtained_sources=()):
    # Reload under the same lock used by source snapshot and workspace writers.
    case = load_case_snapshot(case.case_id) or case
    current = get_issue_workspaces(case)
    if issue not in current.workspaces or receipt["issue"] != issue:
        raise AppError("unknown_issue", "Investigation Issue is no longer available", 409)
    state = current.workspaces[issue].reasoning
    previous = state.investigation_runs.get(run_id)
    if previous:
        return previous.model_dump(mode="json")
    if state.model_dump(mode="json") != receipt["baseline"]:
        raise AppError("stale_reasoning_proposal", "Reasoning changed during investigation; no changes were committed", 409)
    if [evidence_hash(case, proposal_material_ids(p)) for p in proposals] != receipt["evidence_hashes"]:
        raise AppError("stale_reasoning_proposal", "Evidence changed during investigation; no changes were committed", 409)
    units, files = {u.id: u for u in case.info_units}, {f.file_id: f for f in case.files}
    documents = {}
    # Source integrity belongs to the referenced InfoUnits, not Proposal pointers.
    for uid in dict.fromkeys(uid for p in proposals for uid in proposal_material_ids(p)):
        if uid not in units:
            raise AppError("stale_reasoning_proposal", "Referenced InfoUnit no longer exists", 409)
        for ref in units[uid].source_refs:
            file = files.get(ref.file_id)
            if file is None or ref.source_kind != file.source_kind:
                raise AppError("stale_reasoning_proposal", "Source metadata no longer matches the case", 409)
            if ref.file_id not in documents:
                documents[ref.file_id] = read_parsed_document(case.case_id, file).text
            if ref.end > len(documents[ref.file_id]) or documents[ref.file_id][ref.start:ref.end] != ref.quote:
                raise AppError("stale_reasoning_proposal", "Source quote no longer matches cached TXT", 409)
    if validate:
        validate(case)
    staged, changes = stage_proposals(case, issue, proposals, current, run_id)
    if not changes:
        return {"status": "no_change", "changes": []}
    # Obtained source snapshots describe this run's reads, never per-Proposal support.
    record = ReasoningRun(changes=changes, proposals=proposals, evidence=list(obtained_sources))
    staged.workspaces[issue].reasoning.investigation_runs[run_id] = record
    # The journal and all object edits share one atomic file replacement.
    persist_issue_workspaces(case, staged)
    return record.model_dump(mode="json")


@workspace_transaction
def retry_investigation(case, issue, run_id):
    """Retry only a server-audited normal Finish, without any model call or rebase."""
    try:
        audit = json.loads(artifact_path(case.case_id, f"runs/{run_id}.output.json").read_text(encoding="utf-8"))
        result, receipt = audit["result"], audit["proposal_receipt"]
        if (audit["run_id"] != run_id or result["run_id"] != run_id or receipt["issue"] != issue
                or audit["status"] != "success" or audit["stop_reason"] != "finish_investigation"
                or result["summary_status"] != "completed"
                or "baseline" not in receipt):
            raise ValueError("Only a normally finished current-contract run can be retried")
        proposals = [PROPOSAL_ADAPTER.validate_python(read_stored_proposal(p)) for p in result["proposals"]]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise AppError("invalid_investigation_retry", "No normally finished proposal batch can be recovered", 409) from exc
    record = commit_investigation(case, issue, run_id, proposals, receipt,
        obtained_sources=audit.get("knowledge_state", {}).get("source_refs", {}).values())
    # Keep the original output immutable; the atomic workspace journal records recovery.
    return {"workspaces": get_issue_workspaces(case), "workspace_commit": record}


@workspace_transaction
def reverse_investigation(case, issue, run_id, *, restore=False):
    current = get_issue_workspaces(case)
    if issue not in current.workspaces:
        raise AppError("unknown_issue", "Issue does not belong to the case", 409)
    workspace = current.workspaces[issue]
    record = workspace.reasoning.investigation_runs.get(run_id)
    if record is None:
        raise AppError("unknown_investigation_change", "No committed changes exist for this run", 404)
    desired = "applied" if restore else "undone"
    if record.status == desired:
        return {"workspaces": current, "workspace_commit": record.model_dump(mode="json")}
    objects = _objects(workspace.reasoning)
    for change in record.changes:
        expected = change.before if restore else change.after
        if objects.get((change.collection, change.object_id)) != expected:
            raise AppError("stale_investigation_change", "A changed object was edited later; undo/restore would overwrite it", 409)
    for change in record.changes:
        collection = getattr(workspace.reasoning, change.collection)
        existing = next((obj for obj in collection if obj.id == change.object_id), None)
        value = change.after if restore else change.before
        if value is None:
            collection[:] = [obj for obj in collection if obj.id != change.object_id]
        elif existing is not None:
            collection[collection.index(existing)] = value.model_copy(deep=True)
        else:
            collection.append(value.model_copy(deep=True))
    record.status = desired
    # Revalidate the whole graph: later objects may depend on a run's new parents.
    try:
        workspace.reasoning = ReasoningState.model_validate(workspace.reasoning.model_dump())
    except ValueError as exc:
        raise AppError("stale_investigation_change", "Later reasoning depends on these objects", 409) from exc
    known = {u.id for u in case.info_units}
    if any(link.info_unit_id not in known for link in workspace.reasoning.links) or any(
            not set(g.basis_info_unit_ids + g.addressing_info_unit_ids) <= known for g in workspace.reasoning.gaps):
        raise AppError("stale_investigation_change", "Referenced case material no longer exists", 409)
    present = {obj.id for obj in [*workspace.reasoning.hypotheses, *workspace.reasoning.gaps]}
    deleted = {c.object_id for c in record.changes if c.collection != "links" and c.object_id not in present}
    canvas = workspace.canvas
    canvas.visible_node_ids = [uid for uid in canvas.visible_node_ids if uid not in deleted]
    canvas.selected_node_ids = [uid for uid in canvas.selected_node_ids if uid not in deleted]
    if canvas.focused_node_id in deleted:
        canvas.focused_node_id = None
    for uid in deleted:
        canvas.node_positions.pop(uid, None)
        canvas.display_levels.pop(uid, None)
    for index, change in enumerate(record.changes):
        if change.collection != "links" and change.object_id in present and change.object_id not in canvas.visible_node_ids:
            canvas.visible_node_ids.append(change.object_id)
            canvas.node_positions.setdefault(change.object_id, Point(x=800, y=160 + index * 220))
    persist_issue_workspaces(case, current)
    return {"workspaces": current, "workspace_commit": record.model_dump(mode="json")}
