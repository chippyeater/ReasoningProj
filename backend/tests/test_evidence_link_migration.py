"""Unsupported stored link meanings fail explicitly without migration."""
import json
import unittest

import test_reasoning_proposals as fixture
from app.case_store import case_dir
from app.issue_workspaces import get_issue_workspaces
from app.errors import AppError


class TestEvidenceLinkMigration(unittest.TestCase):
    setUp = fixture.TestReasoningProposals.setUp
    tearDown = fixture.TestReasoningProposals.tearDown
    edit = fixture.TestReasoningProposals.edit
    hypothesis = fixture.TestReasoningProposals.hypothesis
    investigate = fixture.TestReasoningProposals.investigate

    def test_unsupported_stored_link_is_rejected_without_rewriting(self):
        hid = self.hypothesis()
        self.edit(hypothesis={"id": hid, "content": "用户已判断的假设", "judgment": "verified"})
        response = self.edit(link={"hypothesis_id": hid, "info_unit_id": "U029", "rationale": "材料有关，方向不明确。"})
        self.assertEqual(response.status_code, 200, response.text)
        lid = response.json()["object_id"]
        path = case_dir(self.case.case_id) / "issue_workspaces.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["workspaces"][self.issue]["reasoning"]["links"][0]["meaning"] = "verify"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        before = path.read_bytes()
        with self.assertRaises(AppError) as caught:
            get_issue_workspaces(self.case)
        self.assertEqual(caught.exception.code, "invalid_issue_workspaces")
        self.assertEqual(path.read_bytes(), before)

    def test_historical_audit_has_no_replay_entry_and_is_not_rewritten(self):
        hid = self.hypothesis()
        proposal = {**fixture.suggestion("evidence_link", hid), "meaning": "related"}
        response, _ = self.investigate([proposal], [hid])
        self.assertEqual(response.status_code, 200, response.text)
        run_id = response.json()["run_id"]
        path = case_dir(self.case.case_id) / f"runs/{run_id}.output.json"
        audit = json.loads(path.read_text(encoding="utf-8"))
        audit["result"]["proposals"][0]["meaning"] = "verify"
        path.write_text(json.dumps(audit, ensure_ascii=False), encoding="utf-8")
        before = path.read_bytes()
        rejected = self.client.post(f"/api/cases/{self.case.case_id}/reasoning-proposals/accept", json={"issue": self.issue, "run_id": run_id})
        self.assertEqual(rejected.status_code, 404, rejected.text)
        link = get_issue_workspaces(self.case).workspaces[self.issue].reasoning.links[0]
        self.assertEqual(link.meaning, "related")
        self.assertEqual(path.read_bytes(), before)
