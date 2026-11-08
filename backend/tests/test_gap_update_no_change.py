"""Existing Gap update semantics and actionable no-change feedback, fully offline."""
import json
import unittest
from unittest.mock import patch

import test_reasoning_proposals as fixture
from app import reasoning_proposals
from app.case_store import case_dir
from app.issue_workspaces import get_issue_workspaces


class TestGapUpdateNoChange(unittest.TestCase):
    setUp = fixture.TestReasoningProposals.setUp
    tearDown = fixture.TestReasoningProposals.tearDown
    edit = fixture.TestReasoningProposals.edit
    hypothesis = fixture.TestReasoningProposals.hypothesis
    investigate = fixture.TestReasoningProposals.investigate

    def state(self):
        return get_issue_workspaces(self.case).workspaces[self.issue].reasoning

    def raw(self):
        return (case_dir(self.case.case_id) / "issue_workspaces.json").read_bytes()

    def gap(self):
        hid = self.hypothesis()
        values = {"content": "实际收租是否连续？", "hypothesis_ids": [hid],
            "basis_info_unit_ids": ["U026"], "status": "open"}
        response = self.edit(gap=values)
        self.assertEqual(response.status_code, 200, response.text)
        gid = response.json()["object_id"]
        response = self.edit(gap={**values, "id": gid, "addressing_info_unit_ids": ["U029"]})
        self.assertEqual(response.status_code, 200, response.text)
        return gid

    def update(self, gid, **overrides):
        return {"kind": "evidence_gap_update", "gap_id": gid, "status": "open",
            "addressing_info_unit_ids": ["U029"], "rationale": "现有陈述只部分回应，仍需核实实收。", **overrides}

    def feedback(self, result):
        path = case_dir(self.case.case_id) / f"runs/{result['run_id']}.output.json"
        return [c["result"] for c in json.loads(path.read_text(encoding="utf-8"))["tool_calls"]
                if c["name"] == "propose_evidence_gap_update"]

    def test_open_with_new_addressing_unit_commits_without_changing_other_semantics(self):
        gid = self.gap()
        before, saved = self.state().model_copy(deep=True), self.raw()
        proposal = self.update(gid, addressing_info_unit_ids=["U036"])
        with patch.object(reasoning_proposals, "persist_issue_workspaces", wraps=reasoning_proposals.persist_issue_workspaces) as save:
            response, _ = self.investigate([proposal], [gid], read_ids=["U036"],
                before_finish=lambda: self.assertEqual(self.raw(), saved))
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(save.call_count, 1)
        self.assertEqual(result["workspace_commit"]["status"], "applied")
        self.assertEqual(len(result["proposals"]), 1)
        after = self.state()
        self.assertEqual(after.gaps[0].id, gid)
        self.assertEqual(after.gaps[0].status, "open")
        self.assertEqual(after.gaps[0].addressing_info_unit_ids, ["U029", "U036"])
        self.assertEqual(after.gaps[0].content, before.gaps[0].content)
        self.assertEqual(after.gaps[0].basis_info_unit_ids, before.gaps[0].basis_info_unit_ids)
        self.assertEqual(after.gaps[0].hypothesis_ids, before.gaps[0].hypothesis_ids)
        self.assertEqual([(h.id, h.content, h.judgment, h.work_status) for h in after.hypotheses],
                         [(h.id, h.content, h.judgment, h.work_status) for h in before.hypotheses])
        self.assertEqual(after.links, before.links)

    def test_same_status_materials_and_rationale_changes_remain_noops(self):
        gid = self.gap()
        saved = self.raw()
        for overrides in ({}, {"rationale": "换个理由仍不构成实际变化。"}):
            with self.subTest(overrides=overrides), patch.object(reasoning_proposals, "persist_issue_workspaces") as save:
                response, _ = self.investigate([self.update(gid, **overrides)], [gid])
                save.assert_not_called()
            self.assertEqual(response.status_code, 200, response.text)
            result = response.json()
            self.assertEqual(result["proposals"], [])
            self.assertEqual(result["workspace_commit"]["status"], "no_change")
            feedback = self.feedback(result)[0]
            self.assertEqual(feedback["status"], "ok")
            self.assertTrue(feedback["no_change"])
            self.assertFalse(feedback["applied"])
            self.assertEqual(set(feedback), {"status", "applied", "no_change", "message", "next_step"})
            for explanation in ("status is unchanged", "already associated", "rationale",
                                "No change was applied", "unread Goal-relevant", "finish_investigation", "Do not resubmit"):
                self.assertIn(explanation, feedback["message"])
            self.assertEqual(self.raw(), saved)

    def test_status_only_change_with_existing_material_is_valid(self):
        gid = self.gap()
        response, _ = self.investigate([self.update(gid, status="resolved")], [gid])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["workspace_commit"]["status"], "applied")
        self.assertEqual(self.state().gaps[0].status, "resolved")
        self.assertEqual(self.state().gaps[0].addressing_info_unit_ids, ["U029"])

    def test_repeating_new_material_in_the_same_run_does_not_stage_another_update(self):
        gid = self.gap()
        proposal = self.update(gid, addressing_info_unit_ids=["U036"])
        response, _ = self.investigate([proposal, {**proposal, "rationale": "仅改写已有理由。"}], [gid], read_ids=["U036"])
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(len(result["proposals"]), 1)
        self.assertEqual(result["workspace_commit"]["status"], "applied")
        self.assertTrue(self.feedback(result)[1]["no_change"])
        self.assertEqual(self.state().gaps[0].addressing_info_unit_ids, ["U029", "U036"])
