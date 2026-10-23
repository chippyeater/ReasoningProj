"""Human reasoning edits on Case A, without modifying business data."""
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import test_investigation_agent as fixtures
from app import main
from app.case_store import case_dir
from app.issue_workspaces import get_issue_workspaces


class TestReasoningWorkspace(unittest.TestCase):
    setUp = fixtures.TestInvestigationAgent.setUp

    def setUp(self):
        fixtures.TestInvestigationAgent.setUp(self)
        override = patch.object(main, "require_case", return_value=self.case)
        override.start()
        self.addCleanup(override.stop)
        self.client = TestClient(main.app)
        self.url = f"/api/cases/{self.case.case_id}/issue-reasoning"
        self.issue = self.case.issue_set[1]
        self.before = self.case.model_dump_json()

    def tearDown(self):
        self.assertEqual(self.case.model_dump_json(), self.before)
        for name, raw in self.fixture.before_files.items():
            self.assertEqual(self.fixture.disk_snapshot()[name], raw)

    def edit(self, **values):
        return self.client.patch(self.url, json={"issue": self.issue, **values})

    def hypothesis(self, content="房东可能存在实际空置损失"):
        result = self.edit(hypothesis={"content": content}, position={"x": 900, "y": 300})
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()["object_id"]

    def test_edit_link_gap_and_restore_without_business_copies(self):
        a, b = self.hypothesis(), self.hypothesis("房东可能没有实际空置损失")
        for uid, target, meaning in (("U029", a, "oppose"), ("U029", b, "support"), ("U026", a, "verify")):
            result = self.edit(link={"hypothesis_id": target, "info_unit_id": uid, "meaning": meaning, "rationale": "材料陈述，尚需核实"})
            self.assertEqual(result.status_code, 200, result.text)
        gap = self.edit(gap={"content": "是否存在替租人的实际入住记录？", "hypothesis_ids": [a, b], "info_unit_ids": ["U026"]})
        self.assertEqual(gap.status_code, 200, gap.text)
        gid = gap.json()["object_id"]
        self.assertEqual(self.edit(gap={"id": gid, "content": "核对实际入住记录", "hypothesis_ids": [a, b], "status": "resolved"}).status_code, 200)
        self.assertEqual(self.edit(hypothesis={"id": a, "content": "损失金额仍需核实", "judgment": "unverified", "work_status": "deprecated"}).status_code, 200)
        state = get_issue_workspaces(self.case)
        workspace = state.workspaces[self.issue]
        self.assertEqual(workspace.reasoning.hypotheses[0].judgment, "unverified")
        self.assertEqual(workspace.reasoning.hypotheses[0].work_status, "deprecated")
        self.assertEqual(workspace.canvas.node_positions[a].x, 900)
        self.assertFalse(state.workspaces[self.case.core_issue].reasoning.hypotheses)
        self.assertEqual(len(workspace.reasoning.links), 3)
        link = workspace.reasoning.links[0]
        self.assertEqual(self.edit(link={"id": link.id, "hypothesis_id": a, "info_unit_id": "U029", "meaning": "verify", "rationale": "暂不判断支持力度"}).status_code, 200)
        self.assertEqual(self.edit(unlink_id=link.id).status_code, 200)
        self.assertEqual(self.edit(hypothesis={"id": a, "content": "损失金额仍需核实", "work_status": "active"}).status_code, 200)
        self.assertEqual(get_issue_workspaces(self.case).workspaces[self.issue].reasoning.hypotheses[0].work_status, "active")

    def test_invalid_refs_duplicates_and_write_failure_leave_file_unchanged(self):
        hid = self.hypothesis()
        path = case_dir(self.case.case_id) / "issue_workspaces.json"
        before = path.read_bytes()
        for payload in (
            {"link": {"hypothesis_id": hid, "info_unit_id": "foreign", "meaning": "support", "rationale": "未知材料"}},
            {"gap": {"content": "未知假设", "hypothesis_ids": ["foreign"]}},
            {"hypothesis": {"id": "foreign", "content": "未知假设"}},
        ):
            self.assertGreaterEqual(self.edit(**payload).status_code, 400)
            self.assertEqual(path.read_bytes(), before)
        link = {"hypothesis_id": hid, "info_unit_id": "U029", "meaning": "support", "rationale": "材料陈述"}
        self.assertEqual(self.edit(link=link).status_code, 200)
        self.assertEqual(self.edit(link=link).status_code, 409)
        before = path.read_bytes()
        with patch("app.issue_workspaces.write_json", side_effect=OSError("disk full")):
            self.assertEqual(self.edit(hypothesis={"id": hid, "content": "不能保存的更改"}).status_code, 500)
        self.assertEqual(path.read_bytes(), before)
        # Cross-Issue IDs cannot be smuggled into a canvas.
        foreign = get_issue_workspaces(self.case).workspaces[self.case.core_issue].canvas.model_dump(mode="json")
        foreign["visible_node_ids"].append(hid)
        response = self.client.patch(f"/api/cases/{self.case.case_id}/issue-workspaces", json={"issue": self.case.core_issue, "canvas": foreign})
        self.assertEqual(response.status_code, 422)

    def test_legacy_workspace_and_referenced_evidence_are_protected(self):
        self.hypothesis()
        path = case_dir(self.case.case_id) / "issue_workspaces.json"
        import json
        payload = json.loads(path.read_text(encoding="utf-8"))
        for workspace in payload["workspaces"].values():
            workspace.pop("reasoning", None)
        path.write_text(json.dumps(payload), encoding="utf-8")
        self.assertFalse(get_issue_workspaces(self.case).workspaces[self.issue].reasoning.hypotheses)
        hid = self.hypothesis()
        self.edit(link={"hypothesis_id": hid, "info_unit_id": "U029", "meaning": "verify", "rationale": "需核实"})
        from app.issue_workspaces import reasoning_references
        self.assertEqual(reasoning_references(self.case, "U029")[0]["kind"], "hypothesis_evidence")
        rejected = self.client.delete(f"/api/cases/{self.case.case_id}/info-units/U029")
        self.assertEqual(rejected.status_code, 409)
        self.assertTrue(any(ref["kind"] == "hypothesis_evidence" for ref in rejected.json()["detail"]["references"]))
