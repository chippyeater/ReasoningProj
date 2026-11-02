"""Independent approvals share expected revisions, without trusting external writes."""
from itertools import permutations
import json
import unittest
from unittest.mock import patch

import test_reasoning_proposals as fixture
from app.case_store import case_dir
from app.issue_workspaces import get_issue_workspaces


class TestSameRunProposalAdoption(unittest.TestCase):
    setUp = fixture.TestReasoningProposals.setUp
    tearDown = fixture.TestReasoningProposals.tearDown
    edit = fixture.TestReasoningProposals.edit
    hypothesis = fixture.TestReasoningProposals.hypothesis
    investigate = fixture.TestReasoningProposals.investigate
    accept = fixture.TestReasoningProposals.accept

    def state(self):
        return get_issue_workspaces(self.case).workspaces[self.issue].reasoning

    def link(self, hid, uid):
        return {**fixture.suggestion("evidence_link", hid), "info_unit_ids": [uid],
                "evidence_refs": [{"info_unit_id": uid, "source_ref_index": 0}]}

    def gap(self, hids):
        response = self.edit(gap={"content": "新租约是否实际履行？", "hypothesis_ids": hids, "basis_info_unit_ids": ["U026"]})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["object_id"]

    def update(self, gid, uid="U026", status="open"):
        return {"kind": "evidence_gap_update", "gap_id": gid, "addressing_info_unit_ids": [uid], "status": status,
                "rationale": "材料部分回应履行条件，仍需核实。", "evidence_refs": [{"info_unit_id": uid, "source_ref_index": 0}]}

    def run_proposals(self, proposals, selected):
        response, _ = self.investigate(proposals, selected, read_ids=["U026", "U029", "U036"])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(response.json()["proposals"]), len(proposals), response.text)
        return response.json()["run_id"]

    def assert_stale(self, run, index):
        before = (case_dir(self.case.case_id) / "issue_workspaces.json").read_bytes()
        response = self.accept(run, index)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"]["code"], "stale_reasoning_proposal")
        self.assertEqual((case_dir(self.case.case_id) / "issue_workspaces.json").read_bytes(), before)

    def test_three_links_to_existing_hypothesis_in_every_order(self):
        for order in permutations(range(3)):
            with self.subTest(order=order):
                hid = self.hypothesis()
                run = self.run_proposals([self.link(hid, uid) for uid in ("U026", "U029", "U036")], [hid])
                audit = case_dir(self.case.case_id) / f"runs/{run}.output.json"
                before = audit.read_bytes()
                for index in order:
                    response = self.accept(run, index)
                    self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(next(h for h in self.state().hypotheses if h.id == hid).revision, 4)
                self.assertEqual({l.info_unit_id for l in self.state().links if l.hypothesis_id == hid}, {"U026", "U029", "U036"})
                path = case_dir(self.case.case_id) / "issue_workspaces.json"
                saved = path.read_bytes()
                self.assertTrue(self.accept(run, order[0]).json()["already_accepted"])
                self.assertEqual(path.read_bytes(), saved)
                self.assertEqual(audit.read_bytes(), before)

    def test_shared_gap_update_and_links_in_every_order(self):
        for order in permutations(range(3)):
            with self.subTest(order=order):
                hid, other = self.hypothesis(), self.hypothesis("无空置损失的竞争性解释")
                gid = self.gap([hid, other])
                original = next(g for g in self.state().gaps if g.id == gid).model_copy(deep=True)
                run = self.run_proposals([self.update(gid), self.link(hid, "U029"), self.link(hid, "U036")], [gid, hid])
                for index in order:
                    response = self.accept(run, index)
                    self.assertEqual(response.status_code, 200, response.text)
                gap = next(g for g in self.state().gaps if g.id == gid)
                for field in ("id", "content", "basis_info_unit_ids", "hypothesis_ids", "status"):
                    self.assertEqual(getattr(gap, field), getattr(original, field))
                self.assertEqual(gap.addressing_info_unit_ids, ["U026"])
                self.assertEqual(gap.revision, original.revision + 1)
                self.assertEqual(next(h for h in self.state().hypotheses if h.id == hid).revision, 5)
                self.assertEqual(next(h for h in self.state().hypotheses if h.id == other).revision, 3)

    def test_multiple_gap_appends_and_noops_use_actual_revision_effects(self):
        for duplicate in (False, True):
            for order in permutations(range(3)):
                with self.subTest(duplicate=duplicate, order=order):
                    hid = self.hypothesis()
                    gid = self.gap([hid])
                    second = {**self.update(gid, "U026" if duplicate else "U036"), "rationale": "独立核查后仍建议保持原缺口开放。"}
                    run = self.run_proposals([self.update(gid), second,
                                              self.link(hid, "U029")], [gid])
                    for index in order:
                        response = self.accept(run, index)
                        self.assertEqual(response.status_code, 200, response.text)
                    gap = next(g for g in self.state().gaps if g.id == gid)
                    self.assertEqual(set(gap.addressing_info_unit_ids), {"U026"} if duplicate else {"U026", "U036"})
                    self.assertEqual(gap.revision, 2 if duplicate else 3)
                    self.assertEqual(next(h for h in self.state().hypotheses if h.id == hid).revision, 4 if duplicate else 5)

    def test_user_or_other_run_writes_still_invalidate_pending_siblings(self):
        for change in ("hypothesis", "link_edit", "unlink", "gap", "other_run"):
            with self.subTest(change=change):
                hid = self.hypothesis()
                gid = self.gap([hid])
                run = self.run_proposals([self.link(hid, "U029"), self.link(hid, "U036"), self.update(gid)], [gid])
                first = self.accept(run, 0)
                self.assertEqual(first.status_code, 200, first.text)
                if change == "hypothesis":
                    self.assertEqual(self.edit(hypothesis={"id": hid, "content": "用户修正后的解释"}).status_code, 200)
                elif change == "link_edit":
                    self.assertEqual(self.edit(link={"id": first.json()["object_id"], "hypothesis_id": hid,
                        "info_unit_id": "U029", "meaning": "related", "rationale": "用户修正关联"}).status_code, 200)
                elif change == "unlink":
                    self.assertEqual(self.edit(unlink_id=first.json()["object_id"]).status_code, 200)
                elif change == "gap":
                    self.assertEqual(self.edit(gap={"id": gid, "content": "用户修正条件", "hypothesis_ids": [hid]}).status_code, 200)
                else:
                    other = self.run_proposals([self.link(hid, "U026")], [hid])
                    self.assertEqual(self.accept(other, 0).status_code, 200)
                self.assert_stale(run, 1)
                self.assert_stale(run, 2)

    def test_pending_and_already_adopted_material_changes_are_rejected(self):
        for uid in ("U029", "U036"):
            with self.subTest(uid=uid):
                hid = self.hypothesis()
                run = self.run_proposals([self.link(hid, "U029"), self.link(hid, "U036")], [hid])
                self.assertEqual(self.accept(run, 0).status_code, 200)
                unit = next(u for u in self.case.info_units if u.id == uid)
                original = unit.content
                try:
                    unit.content = "用户修正了相关材料"
                    self.assert_stale(run, 1)
                finally:
                    unit.content = original

    def test_conflicting_gap_status_and_duplicate_link_are_not_rebased(self):
        for order in ((0, 1), (1, 0)):
            hid = self.hypothesis()
            gid = self.gap([hid])
            run = self.run_proposals([self.update(gid, status="open"), self.update(gid, "U036", "resolved")], [gid])
            self.assertEqual(self.accept(run, order[0]).status_code, 200)
            self.assert_stale(run, order[1])
        hid = self.hypothesis()
        run = self.run_proposals([self.link(hid, "U029"), {**self.link(hid, "U029"), "meaning": "support"}], [hid])
        self.assertEqual(self.accept(run, 0).status_code, 200)
        response = self.accept(run, 1)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["code"], "duplicate_evidence_link")

    def test_failed_sibling_save_does_not_grant_revision_credit(self):
        hid = self.hypothesis()
        run = self.run_proposals([self.link(hid, "U029"), self.link(hid, "U036")], [hid])
        with patch("app.issue_workspaces.write_json", side_effect=OSError("disk full")):
            self.assertEqual(self.accept(run, 0).status_code, 500)
        self.assertEqual(self.accept(run, 1).status_code, 200)
        self.assertEqual(self.accept(run, 0).status_code, 200)

    def test_noop_approval_does_not_mask_user_revision(self):
        hid = self.hypothesis()
        gid = self.gap([hid])
        run = self.run_proposals([self.update(gid), {**self.update(gid), "rationale": "再次核查仍仅部分回应。"},
                                  self.link(hid, "U029")], [gid])
        self.assertEqual(self.accept(run, 0).status_code, 200)
        self.assertEqual(self.accept(run, 1).status_code, 200)
        self.assertEqual(self.edit(hypothesis={"id": hid, "content": "用户中途修改假设"}).status_code, 200)
        self.assert_stale(run, 2)

    def test_existing_link_receipt_revisions_are_still_checked(self):
        hid = self.hypothesis()
        run = self.run_proposals([self.link(hid, uid) for uid in ("U026", "U029", "U036")], [hid])
        path = case_dir(self.case.case_id) / f"runs/{run}.output.json"
        audit = json.loads(path.read_text(encoding="utf-8"))
        audit["proposal_receipt"].pop("target_state")  # Isolated pre-snapshot receipt.
        path.write_text(json.dumps(audit), encoding="utf-8")
        self.assertEqual(self.accept(run, 1).status_code, 200)
        self.assertEqual(self.accept(run, 0).status_code, 200)
        self.assertEqual(self.edit(hypothesis={"id": hid, "content": "用户修正旧运行目标"}).status_code, 200)
        self.assert_stale(run, 2)
