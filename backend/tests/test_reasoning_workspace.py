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
        for uid, target, meaning in (("U029", a, "oppose"), ("U029", b, "support"), ("U026", a, "related")):
            result = self.edit(link={"hypothesis_id": target, "info_unit_id": uid, "meaning": meaning, "rationale": "材料陈述，尚需核实"})
            self.assertEqual(result.status_code, 200, result.text)
        gap = self.edit(gap={"content": "是否存在替租人的实际入住记录？", "hypothesis_ids": [a, b], "basis_info_unit_ids": ["U026"]})
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
        self.assertEqual(self.edit(link={"id": link.id, "hypothesis_id": a, "info_unit_id": "U029", "meaning": "related", "rationale": "暂不判断支持力度"}).status_code, 200)
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
        self.edit(link={"hypothesis_id": hid, "info_unit_id": "U029", "meaning": "related", "rationale": "需核实"})
        from app.issue_workspaces import reasoning_references
        self.assertEqual(reasoning_references(self.case, "U029")[0]["kind"], "hypothesis_evidence")
        rejected = self.client.delete(f"/api/cases/{self.case.case_id}/info-units/U029")
        self.assertEqual(rejected.status_code, 409)
        self.assertTrue(any(ref["kind"] == "hypothesis_evidence" for ref in rejected.json()["detail"]["references"]))

    def test_gap_addressing_add_remove_preserves_gap_and_revises_shared_hypotheses(self):
        hids = [self.hypothesis(), self.hypothesis("不存在空置损失的竞争性解释")]
        draft = {"content": "实际收租是否连续？", "hypothesis_ids": hids,
                 "basis_info_unit_ids": ["U026"], "status": "resolved"}
        created = self.edit(gap=draft)
        self.assertEqual(created.status_code, 200, created.text)
        gid = created.json()["object_id"]
        state = get_issue_workspaces(self.case).workspaces[self.issue].reasoning
        for changes, expected in (
            ({"addressing_info_unit_ids": ["U029"]}, ["U029"]),
            ({"addressing_info_unit_ids": ["U036", "U029", "U036"]}, ["U029", "U036"]),
            ({"remove_addressing_info_unit_ids": ["U029"]}, ["U036"]),
            ({"remove_addressing_info_unit_ids": ["U036"]}, []),
        ):
            with self.subTest(changes=changes):
                previous = state.model_copy(deep=True)
                # Omitted basis/status must survive an addressing-only edit.
                response = self.edit(gap={"id": gid, "content": draft["content"], "hypothesis_ids": hids, **changes})
                self.assertEqual(response.status_code, 200, response.text)
                state = get_issue_workspaces(self.case).workspaces[self.issue].reasoning
                self.assertEqual(len(state.gaps), 1)
                gap = state.gaps[0]
                self.assertEqual(gap.id, gid)
                for field, value in draft.items():
                    self.assertEqual(getattr(gap, field), value)
                self.assertEqual(gap.addressing_info_unit_ids, expected)
                self.assertEqual(gap.revision, previous.gaps[0].revision + 1)
                self.assertNotEqual(gap.updated_at, previous.gaps[0].updated_at)
                self.assertEqual(gap.last_edited_by, "user")
                for actual, before in zip(state.hypotheses, previous.hypotheses):
                    self.assertEqual(actual.revision, before.revision + 1)
                    self.assertNotEqual(actual.updated_at, before.updated_at)
                    self.assertEqual(actual.last_edited_by, "user")
                self.assertNotIn("remove_addressing_info_unit_ids", gap.model_dump())

    def test_gap_addressing_noops_preserve_all_revision_metadata(self):
        from app.reasoning_models import ReasoningEdit
        from app.reasoning_service import edit_reasoning
        hid = self.hypothesis()
        draft = {"content": "实际收租是否连续？", "hypothesis_ids": [hid], "basis_info_unit_ids": ["U026"]}
        gid = self.edit(gap=draft).json()["object_id"]
        edit_reasoning(self.case, ReasoningEdit(issue=self.issue, gap={"id": gid, **draft,
            "addressing_info_unit_ids": ["U029"]}), proposal_key="test-agent-update")
        before = get_issue_workspaces(self.case).workspaces[self.issue].reasoning.model_dump()
        self.assertEqual(before["gaps"][0]["last_edited_by"], "agent_accepted")
        for changes in ({}, {"addressing_info_unit_ids": []}, {"remove_addressing_info_unit_ids": []},
                        {"addressing_info_unit_ids": ["U029", "U029"]}):
            response = self.edit(gap={"id": gid, **draft, **changes})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(get_issue_workspaces(self.case).workspaces[self.issue].reasoning.model_dump(), before)

    def test_gap_addressing_invalid_edits_and_save_failure_are_atomic(self):
        hid = self.hypothesis()
        draft = {"content": "实际收租是否连续？", "hypothesis_ids": [hid], "basis_info_unit_ids": ["U026"]}
        gid = self.edit(gap=draft).json()["object_id"]
        self.assertEqual(self.edit(gap={"id": gid, **draft, "addressing_info_unit_ids": ["U029"]}).status_code, 200)
        path = case_dir(self.case.case_id) / "issue_workspaces.json"
        before = path.read_bytes()
        for values in (
            {"id": gid, "addressing_info_unit_ids": ["foreign"]},
            {"id": gid, "remove_addressing_info_unit_ids": ["U036"]},
            {"id": gid, "remove_addressing_info_unit_ids": ["foreign"]},
            {"id": gid, "addressing_info_unit_ids": ["U029"], "remove_addressing_info_unit_ids": ["U029"]},
            {"id": gid, "addressing_info_unit_ids": ["foreign"], "remove_addressing_info_unit_ids": ["U029"]},
            {"addressing_info_unit_ids": ["U029"]},
            {"remove_addressing_info_unit_ids": ["U029"]},
        ):
            with self.subTest(values=values):
                response = self.edit(gap={**draft, **values})
                self.assertGreaterEqual(response.status_code, 400, response.text)
                self.assertEqual(path.read_bytes(), before)
        change = {"id": gid, **draft, "addressing_info_unit_ids": ["U036"], "remove_addressing_info_unit_ids": ["U029"]}
        with patch("app.issue_workspaces.write_json", side_effect=OSError("disk full")):
            self.assertEqual(self.edit(gap=change).status_code, 500)
        self.assertEqual(path.read_bytes(), before)
        old = get_issue_workspaces(self.case).workspaces[self.issue].reasoning
        self.assertEqual(self.edit(gap=change).status_code, 200)
        saved = get_issue_workspaces(self.case).workspaces[self.issue].reasoning
        self.assertEqual(saved.gaps[0].addressing_info_unit_ids, ["U036"])
        self.assertEqual(saved.gaps[0].revision, old.gaps[0].revision + 1)
        self.assertEqual(saved.hypotheses[0].revision, old.hypotheses[0].revision + 1)

    def test_gap_addressing_changes_revise_old_and_new_hypotheses_once(self):
        hids = [self.hypothesis(f"待核查解释{i}") for i in range(3)]
        draft = {"content": "实际收租是否连续？", "hypothesis_ids": hids[:2], "basis_info_unit_ids": ["U026"]}
        gid = self.edit(gap=draft).json()["object_id"]
        self.edit(gap={"id": gid, **draft, "addressing_info_unit_ids": ["U029"]})
        before = get_issue_workspaces(self.case).workspaces[self.issue].reasoning
        response = self.edit(gap={"id": gid, **draft, "hypothesis_ids": hids[1:],
            "addressing_info_unit_ids": ["U036"], "remove_addressing_info_unit_ids": ["U029"]})
        self.assertEqual(response.status_code, 200, response.text)
        state = get_issue_workspaces(self.case).workspaces[self.issue].reasoning
        self.assertEqual(state.gaps[0].hypothesis_ids, hids[1:])
        self.assertEqual(state.gaps[0].revision, before.gaps[0].revision + 1)
        self.assertEqual(state.gaps[0].addressing_info_unit_ids, ["U036"])
        self.assertEqual([h.revision for h in state.hypotheses], [h.revision + 1 for h in before.hypotheses])


class TestReasoningDeletion(unittest.TestCase):
    setUp = TestReasoningWorkspace.setUp
    tearDown = TestReasoningWorkspace.tearDown
    edit = TestReasoningWorkspace.edit
    hypothesis = TestReasoningWorkspace.hypothesis

    def workspace(self):
        return get_issue_workspaces(self.case).workspaces[self.issue]

    def gap(self, hids, content="待核实的共同事实"):
        result = self.edit(gap={"content": content, "hypothesis_ids": hids,
                                "basis_info_unit_ids": ["U026"], "status": "resolved"})
        self.assertEqual(result.status_code, 200, result.text)
        gid = result.json()["object_id"]
        self.assertEqual(self.edit(gap={"id": gid, "content": content, "hypothesis_ids": hids,
                                       "addressing_info_unit_ids": ["U029"]}).status_code, 200)
        return gid

    def select(self, ids):
        canvas = self.workspace().canvas.model_dump(mode="json")
        canvas.update(selected_node_ids=ids, focused_node_id=ids[0])
        canvas["display_levels"].update({uid: 2 for uid in ids})
        result = self.client.patch(f"/api/cases/{self.case.case_id}/issue-workspaces",
                                   json={"issue": self.issue, "canvas": canvas})
        self.assertEqual(result.status_code, 200, result.text)

    def assert_absent_from_canvas(self, ids):
        canvas = self.workspace().canvas
        for uid in ids:
            self.assertNotIn(uid, canvas.visible_node_ids)
            self.assertNotIn(uid, canvas.selected_node_ids)
            self.assertNotIn(uid, canvas.node_positions)
            self.assertNotIn(uid, canvas.display_levels)
            self.assertNotEqual(uid, canvas.focused_node_id)

    def test_delete_gap_keeps_hypotheses_evidence_and_other_issue(self):
        hids = [self.hypothesis(), self.hypothesis("竞争性解释")]
        gid = self.gap(hids)
        self.edit(link={"hypothesis_id": hids[0], "info_unit_id": "U029", "rationale": "保留材料关联"})
        self.select([gid, hids[0]])
        before = self.workspace().reasoning
        other = get_issue_workspaces(self.case).workspaces[self.case.core_issue]
        response = self.edit(delete_gap_id=gid)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["object_id"], gid)
        state = self.workspace().reasoning
        self.assertEqual(state.gaps, [])
        self.assertEqual(state.links, before.links)
        self.assertEqual([h.id for h in state.hypotheses], hids)
        self.assertEqual([h.revision for h in state.hypotheses], [h.revision + 1 for h in before.hypotheses])
        self.assertEqual(get_issue_workspaces(self.case).workspaces[self.case.core_issue], other)
        self.assert_absent_from_canvas([gid])
        self.assertIn("U029", self.workspace().canvas.visible_node_ids)

    def test_delete_hypothesis_cleans_exclusive_objects_and_preserves_shared_gaps(self):
        a, b, c = [self.hypothesis(f"解释{i}") for i in range(3)]
        exclusive = self.gap([a], "仅属于被删除假设的缺口")
        shared = [self.gap([a, b], f"共同缺口{i}") for i in range(2)]
        independent = self.gap([c], "无关缺口")
        for hid in (a, b):
            self.edit(link={"hypothesis_id": hid, "info_unit_id": "U029", "rationale": "保留来源"})
        self.select([a, exclusive, shared[0]])
        before = self.workspace().reasoning
        response = self.edit(delete_hypothesis_id=a)
        self.assertEqual(response.status_code, 200, response.text)
        state = self.workspace().reasoning
        self.assertEqual([h.id for h in state.hypotheses], [b, c])
        self.assertEqual([link.hypothesis_id for link in state.links], [b])
        self.assertNotIn(exclusive, [g.id for g in state.gaps])
        for gap in state.gaps:
            old = next(g for g in before.gaps if g.id == gap.id)
            if gap.id == independent:
                self.assertEqual(gap, old)
            else:
                self.assertEqual(gap.hypothesis_ids, [b])
                self.assertEqual(gap.revision, old.revision + 1)
                self.assertEqual(gap.last_edited_by, "user")
                self.assertNotEqual(gap.updated_at, old.updated_at)
                for field in ("id", "content", "basis_info_unit_ids", "addressing_info_unit_ids", "status", "proposal_key"):
                    self.assertEqual(getattr(gap, field), getattr(old, field))
        self.assertEqual(state.hypotheses[0].revision, before.hypotheses[1].revision + 1)
        self.assertEqual(state.hypotheses[1], before.hypotheses[2])
        self.assert_absent_from_canvas([a, exclusive])
        self.assertEqual(self.workspace().canvas.selected_node_ids, [shared[0]])

    def test_invalid_deletions_and_save_failure_do_not_write_partial_data(self):
        hid = self.hypothesis()
        gid = self.gap([hid])
        path = case_dir(self.case.case_id) / "issue_workspaces.json"
        before = path.read_bytes()
        for payload in ({"delete_hypothesis_id": "foreign"}, {"delete_gap_id": "foreign"},
                        {"delete_hypothesis_id": gid}, {"delete_gap_id": hid},
                        {"delete_gap_id": gid, "delete_hypothesis_id": hid}):
            self.assertGreaterEqual(self.edit(**payload).status_code, 400)
            self.assertEqual(path.read_bytes(), before)
        response = self.client.patch(self.url, json={"issue": self.case.core_issue, "delete_hypothesis_id": hid})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(path.read_bytes(), before)
        for payload in ({"delete_gap_id": gid}, {"delete_hypothesis_id": hid}):
            with patch("app.issue_workspaces.write_json", side_effect=OSError("disk full")):
                self.assertEqual(self.edit(**payload).status_code, 500)
            self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.edit(delete_hypothesis_id=hid).status_code, 200)
        after = path.read_bytes()
        self.assertEqual(self.edit(delete_hypothesis_id=hid).status_code, 404)
        self.assertEqual(path.read_bytes(), after)
