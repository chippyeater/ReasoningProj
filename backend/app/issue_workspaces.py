"""Case-local UI state, separate from the shared business snapshot.

Issue text is the stable key while Issue editing is outside the MVP scope.
The legacy workspace is never overwritten by these operations.
"""
import json
from typing import Literal

from pydantic import Field, ValidationError, model_validator

from app.case_store import case_dir, write_json
from app.errors import AppError
from app.schemas import CaseData, ContractModel, Text, WorkspaceState
from app.reasoning_models import ReasoningState

IssueStatus = Literal["pending", "investigating", "closed"]


class IssueCanvas(WorkspaceState):
    visible_node_ids: list[Text]

    @model_validator(mode="after")
    def check_visible(self):
        visible = set(self.visible_node_ids)
        if len(visible) != len(self.visible_node_ids):
            raise ValueError("visible_node_ids must be unique")
        if not set(self.selected_node_ids) <= visible:
            raise ValueError("Selected nodes must be visible")
        if self.focused_node_id is not None and self.focused_node_id not in visible:
            raise ValueError("Focused node must be visible")
        return self


class IssueWorkspace(ContractModel):
    status: IssueStatus = "pending"
    canvas: IssueCanvas
    reasoning: ReasoningState = Field(default_factory=ReasoningState)


class IssueWorkspaces(ContractModel):
    active_issue: Text | None = None
    workspaces: dict[str, IssueWorkspace] = Field(default_factory=dict)


class IssueWorkspacePatch(ContractModel):
    issue: Text
    status: IssueStatus | None = None
    canvas: IssueCanvas | None = None


def issue_keys(case: CaseData) -> list[str]:
    return list(dict.fromkeys(([case.core_issue] if case.core_issue else []) + case.issue_set))


def _node_ids(case: CaseData) -> set[str]:
    return {u.id for u in case.info_units} | {v.id for v in case.reasoning_views}


def _read(case: CaseData) -> IssueWorkspaces:
    path = case_dir(case.case_id) / "issue_workspaces.json"
    if not path.exists():
        return IssueWorkspaces()
    try:
        return IssueWorkspaces.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (ValueError, UnicodeError, ValidationError) as exc:
        raise AppError("invalid_issue_workspaces", "Stored Issue workspace cannot be read; it has not been overwritten", 409) from exc


def get_issue_workspaces(case: CaseData) -> IssueWorkspaces:
    saved = _read(case)
    keys, known = issue_keys(case), _node_ids(case)
    projected = {}
    for key in keys:
        if key in saved.workspaces:
            item = saved.workspaces[key].model_copy(deep=True)
            local_known = known | {obj.id for obj in [*item.reasoning.hypotheses, *item.reasoning.gaps]}
            canvas = item.canvas
            # Deleting shared nodes must not make unrelated Issue layouts unusable.
            canvas.visible_node_ids = [uid for uid in canvas.visible_node_ids if uid in local_known]
            canvas.selected_node_ids = [uid for uid in canvas.selected_node_ids if uid in local_known]
            if canvas.focused_node_id not in local_known:
                canvas.focused_node_id = None
            canvas.node_positions = {uid: p for uid, p in canvas.node_positions.items() if uid in local_known}
            canvas.display_levels = {uid: level for uid, level in canvas.display_levels.items() if uid in local_known}
            if canvas.current_view_id not in {v.id for v in case.reasoning_views}:
                canvas.current_view_id = None
            projected[key] = item
            continue
        legacy = case.workspace_state
        visible = [u.id for u in case.info_units if u.related_issue == key]
        # Reuse the old core layout without changing it or displaying the full case.
        core = key == case.core_issue
        positions = {uid: {"x": 120 + (index % 2) * 440, "y": 120 + (index // 2) * 210}
                     for index, uid in enumerate(visible)}
        if core:
            positions.update(legacy.node_positions)
        projected[key] = IssueWorkspace(canvas=IssueCanvas(
            **{**legacy.model_dump(), "selected_node_ids": [], "focused_node_id": None,
               "current_view_id": None,
               "node_positions": positions,
               "display_levels": legacy.display_levels if core else {},
               "viewport": legacy.viewport if core else {"zoom": 1, "offset_x": 0, "offset_y": 0}},
            visible_node_ids=visible))
    return IssueWorkspaces(active_issue=saved.active_issue if saved.active_issue in keys else (keys[0] if keys else None),
                           workspaces=projected)


def update_issue_workspace(case: CaseData, patch: IssueWorkspacePatch) -> IssueWorkspaces:
    current = get_issue_workspaces(case)
    if patch.issue not in current.workspaces:
        raise AppError("unknown_issue", "Issue does not belong to the current case")
    if patch.canvas is not None:
        canvas = patch.canvas
        refs = set(canvas.visible_node_ids) | canvas.node_positions.keys() | canvas.display_levels.keys()
        reasoning = current.workspaces[patch.issue].reasoning
        known = _node_ids(case) | {obj.id for obj in [*reasoning.hypotheses, *reasoning.gaps]}
        if not refs <= known:
            raise AppError("unknown_workspace_node", "Workspace node does not belong to the current case")
        if canvas.current_view_id is not None and canvas.current_view_id not in {v.id for v in case.reasoning_views}:
            raise AppError("unknown_workspace_view", "Workspace view does not belong to the current case")
        current.workspaces[patch.issue].canvas = canvas
    if patch.status is not None:
        current.workspaces[patch.issue].status = patch.status
    current.active_issue = patch.issue
    persist_issue_workspaces(case, current)
    return current


def persist_issue_workspaces(case: CaseData, current: IssueWorkspaces) -> None:
    # Preserve dormant entries if a later extraction changes issue_set.
    saved = _read(case)
    saved.workspaces.update(current.workspaces)
    saved.active_issue = current.active_issue
    write_json(case_dir(case.case_id) / "issue_workspaces.json", saved.model_dump(mode="json"))


def reasoning_references(case: CaseData, info_unit_id: str) -> list[dict]:
    refs = []
    for issue, workspace in _read(case).workspaces.items():
        for link in workspace.reasoning.links:
            if link.info_unit_id == info_unit_id:
                refs.append({"kind": "hypothesis_evidence", "id": link.id, "issue": issue})
        for gap in workspace.reasoning.gaps:
            if info_unit_id in gap.info_unit_ids:
                refs.append({"kind": "evidence_gap", "id": gap.id, "issue": issue})
    return refs


def investigation_context(case: CaseData, current_issue: str) -> dict:
    state = get_issue_workspaces(case)
    if current_issue not in state.workspaces:
        raise AppError("unknown_issue", "Issue does not belong to the current case")
    # Metadata only. Never serialize canvas state, nodes, relations or operations.
    keys = list(dict.fromkeys([current_issue, *issue_keys(case)]))
    return {"current_issue": current_issue[:500],
            "issues": [{"issue": key[:500], "status": state.workspaces[key].status} for key in keys[:30]],
            "issue_count": len(keys), "has_more_issues": len(keys) > 30}
