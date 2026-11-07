"""Separate Gap basis/addressing roles through edits, reads and one-time migration."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from pydantic import ValidationError

import test_reasoning_workspace as workspace_fixture
import test_reasoning_proposals as proposal_fixture
from app.case_store import case_dir
from app.errors import AppError
from app.investigation_agent import tool_definitions
from app.investigation_state import KnowledgeState, model_context
from app.issue_workspaces import get_issue_workspaces, investigation_context, reasoning_references
from app.reasoning_models import EvidenceGap, PROPOSAL_ADAPTER
from scripts.migrate_gap_references import migrate_workspace
from test_reasoning_proposals import suggestion


class TestGapReferences(unittest.TestCase):
    setUp = workspace_fixture.TestReasoningWorkspace.setUp
    tearDown = workspace_fixture.TestReasoningWorkspace.tearDown
    edit = workspace_fixture.TestReasoningWorkspace.edit
    hypothesis = workspace_fixture.TestReasoningWorkspace.hypothesis

    def create_gap(self, **values):
        hid = self.hypothesis()
        result = self.edit(gap={"content": "新租约是否接续计租？", "hypothesis_ids": [hid],
            "basis_info_unit_ids": ["U029"], **values})
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()["object_id"]

    def gap(self):
        return get_issue_workspaces(self.case).workspaces[self.issue].reasoning.gaps[-1]

    def update_gap(self, **values):
        gap = self.gap()
        return self.edit(gap={"id": gap.id, "content": gap.content, "hypothesis_ids": gap.hypothesis_ids, **values})

    def test_creation_records_only_basis_and_rejects_old_field_or_addressing(self):
        self.create_gap()
        self.assertEqual(self.gap().basis_info_unit_ids, ["U029"])
        self.assertEqual(self.gap().addressing_info_unit_ids, [])
        for values in ({"info_unit_ids": ["U026"]}, {"addressing_info_unit_ids": ["U026"]}):
            result = self.edit(gap={"content": "缺口", "hypothesis_ids": self.gap().hypothesis_ids, **values})
            self.assertEqual(result.status_code, 422, result.text)
        old = self.gap().model_dump(exclude={"basis_info_unit_ids", "addressing_info_unit_ids"})
        with self.assertRaises(ValidationError):
            EvidenceGap.model_validate({**old, "info_unit_ids": ["U029"]})

    def test_addressing_appends_deduplicates_and_preserves_basis_status_and_judgment(self):
        self.create_gap()
        hid = self.gap().hypothesis_ids[0]
        self.assertEqual(self.edit(hypothesis={"id": hid, "content": "尚待检验解释", "judgment": "verified"}).status_code, 200)
        for ids in (["U026"], ["U027", "U026", "U027"], []):
            self.assertEqual(self.update_gap(addressing_info_unit_ids=ids).status_code, 200)
        gap = self.gap()
        self.assertEqual(gap.basis_info_unit_ids, ["U029"])
        self.assertEqual(gap.addressing_info_unit_ids, ["U026", "U027"])
        self.assertEqual(gap.status, "open")
        self.assertEqual(gap.revision, 3)
        hypotheses = get_issue_workspaces(self.case).workspaces[self.issue].reasoning.hypotheses
        self.assertEqual(hypotheses[0].judgment, "verified")
        self.assertEqual(self.update_gap(status="resolved").status_code, 200)
        self.assertEqual(self.update_gap(addressing_info_unit_ids=["U004"]).status_code, 200)
        self.assertEqual(self.gap().status, "resolved")
        self.assertEqual(self.gap().addressing_info_unit_ids, ["U026", "U027", "U004"])

    def test_duplicate_and_empty_addressing_preserve_versions_and_pending_proposal(self):
        self.create_gap()
        self.assertEqual(self.update_gap(addressing_info_unit_ids=["U026"]).status_code, 200)
        hid = self.gap().hypothesis_ids[0]
        result, _ = proposal_fixture.TestReasoningProposals.investigate(self, [suggestion("hypothesis_status", hid)])
        self.assertEqual(result.status_code, 200, result.text)
        path = case_dir(self.case.case_id) / "issue_workspaces.json"
        before = path.read_bytes()
        for ids in (["U026"], [], ["U026", "U026"], []):
            with self.subTest(ids=ids):
                self.assertEqual(self.update_gap(addressing_info_unit_ids=ids).status_code, 200)
                # Includes Gap/Hypothesis revision, timestamps, editors and judgments.
                self.assertEqual(path.read_bytes(), before)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(self.gap().status, "open")
        self.assertEqual(self.update_gap(status="resolved").status_code, 200)
        before = path.read_bytes()
        self.assertEqual(self.update_gap(addressing_info_unit_ids=["U026"]).status_code, 200)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.gap().status, "resolved")

    def test_new_addressing_or_content_change_increments_each_revision_once(self):
        self.create_gap()
        self.update_gap(addressing_info_unit_ids=["U026"])
        for values in ({"addressing_info_unit_ids": ["U026", "U027", "U027"]},
                       {"addressing_info_unit_ids": ["U026", "U027"], "content": "计租起点能否覆盖搬离当日？"}):
            with self.subTest(values=values):
                before = get_issue_workspaces(self.case).workspaces[self.issue].reasoning
                self.assertEqual(self.update_gap(**values).status_code, 200)
                after = get_issue_workspaces(self.case).workspaces[self.issue].reasoning
                self.assertEqual(after.gaps[0].revision, before.gaps[0].revision + 1)
                self.assertEqual(after.hypotheses[0].revision, before.hypotheses[0].revision + 1)
                self.assertNotEqual(after.gaps[0].updated_at, before.gaps[0].updated_at)
                self.assertEqual(after.hypotheses[0].judgment, "unverified")
                self.assertEqual(after.gaps[0].status, "open")
        self.assertEqual(self.gap().addressing_info_unit_ids, ["U026", "U027"])
        self.assertEqual(self.gap().content, "计租起点能否覆盖搬离当日？")

    def test_empty_noop_preserves_accepted_gap_editor_and_timestamp(self):
        hid = self.hypothesis()
        result, _ = proposal_fixture.TestReasoningProposals.investigate(self, [suggestion("evidence_gap", hid)])
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(self.gap().last_edited_by, "agent_accepted")
        path = case_dir(self.case.case_id) / "issue_workspaces.json"
        before = path.read_bytes()
        self.assertEqual(self.update_gap(addressing_info_unit_ids=[]).status_code, 200)
        self.assertEqual(path.read_bytes(), before)

    def test_roles_overlap_only_by_explicit_operation_and_basis_remains_editable(self):
        self.create_gap()
        self.assertEqual(self.update_gap(addressing_info_unit_ids=["U029"]).status_code, 200)
        kinds = {r["kind"] for r in reasoning_references(self.case, "U029")}
        self.assertEqual(kinds, {"evidence_gap_basis", "evidence_gap_addressing"})
        self.assertEqual(self.update_gap(basis_info_unit_ids=["U004"]).status_code, 200)
        self.assertEqual(self.gap().basis_info_unit_ids, ["U004"])
        self.assertEqual(self.gap().addressing_info_unit_ids, ["U029"])

    def test_invalid_case_and_issue_references_leave_saved_state_unchanged(self):
        self.create_gap()
        foreign = self.client.patch(self.url, json={"issue": self.case.core_issue,
            "hypothesis": {"content": "另一个议题中的正式假设"}})
        self.assertEqual(foreign.status_code, 200)
        foreign_hid = foreign.json()["object_id"]
        path = case_dir(self.case.case_id) / "issue_workspaces.json"
        before = path.read_bytes()
        for values in ({"basis_info_unit_ids": ["foreign-case-unit"]},
                       {"addressing_info_unit_ids": ["U026", "foreign-case-unit"]},
                       {"hypothesis_ids": ["foreign-issue-hypothesis"]},
                       {"hypothesis_ids": [foreign_hid]},
                       {"basis_info_unit_ids": ["U029", "U029"]}):
            with self.subTest(values=values):
                self.assertGreaterEqual(self.update_gap(**values).status_code, 400)
                self.assertEqual(path.read_bytes(), before)
        wrong_issue = self.client.patch(self.url, json={"issue": self.case.core_issue,
            "gap": {"id": self.gap().id, "content": "跨议题修改", "hypothesis_ids": self.gap().hypothesis_ids}})
        self.assertEqual(wrong_issue.status_code, 404)
        self.assertEqual(path.read_bytes(), before)

    def test_each_role_blocks_info_unit_deletion_with_specific_reference_kind(self):
        self.create_gap()
        self.update_gap(addressing_info_unit_ids=["U026"])
        for uid, kind in (("U029", "evidence_gap_basis"), ("U026", "evidence_gap_addressing")):
            refs = reasoning_references(self.case, uid)
            self.assertEqual(refs, [{"kind": kind, "id": self.gap().id, "issue": self.issue}])
            result = self.client.delete(f"/api/cases/{self.case.case_id}/info-units/{uid}")
            self.assertEqual(result.status_code, 409)
            self.assertIn(kind, [r["kind"] for r in result.json()["detail"]["references"]])

    def test_context_keeps_roles_separate_with_independent_truncation_flags(self):
        ids = [u.id for u in self.case.info_units]
        gid = self.create_gap(basis_info_unit_ids=ids[:7])
        self.update_gap(addressing_info_unit_ids=ids[7:14])
        work = investigation_context(self.case, self.issue, "核查计租", [gid])
        gap = work["reasoning"]["gaps"][0]
        self.assertEqual(gap["basis_info_unit_ids"], ids[:5])
        self.assertEqual(gap["addressing_info_unit_ids"], ids[7:12])
        self.assertTrue(gap["basis_info_unit_ids_truncated"])
        self.assertTrue(gap["addressing_info_unit_ids_truncated"])
        initial = {"goal": "核查计租", "work_context": work}
        projected = model_context(initial, KnowledgeState(initial))["reasoning"]["gaps"][0]
        self.assertEqual(projected["basis_info_unit_ids"], gap["basis_info_unit_ids"])
        self.assertEqual(projected["addressing_info_unit_ids"], gap["addressing_info_unit_ids"])
        self.assertNotIn("info_unit_ids", projected)
        self.assertEqual(len(self.gap().addressing_info_unit_ids), 7)

    def test_proposal_kinds_have_distinct_fields_and_native_gap_schema_excludes_addressing(self):
        gap = suggestion("evidence_gap", "hyp-existing")
        self.assertEqual(PROPOSAL_ADAPTER.validate_python(gap).basis_info_unit_ids, ["U029"])
        for bad in ({**gap, "info_unit_ids": []}, {**gap, "addressing_info_unit_ids": []},
                    {**suggestion("hypothesis"), "basis_info_unit_ids": []},
                    {**suggestion("hypothesis"), "addressing_info_unit_ids": []}):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                PROPOSAL_ADAPTER.validate_python(bad)
        properties = next(t["function"]["parameters"]["properties"] for t in tool_definitions()
                          if t["function"]["name"] == "propose_evidence_gap")
        self.assertIn("basis_info_unit_ids", properties)
        self.assertNotIn("info_unit_ids", properties)
        self.assertNotIn("addressing_info_unit_ids", properties)

    def test_duplicate_basis_error_points_to_the_native_argument_and_accepts_no_gap(self):
        hid = self.hypothesis()
        proposal = suggestion("evidence_gap", hid)
        proposal["basis_info_unit_ids"] = ["U029", "U029"]
        result, _ = proposal_fixture.TestReasoningProposals.investigate(self, [proposal])
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["proposals"], [])
        audit = json.loads((case_dir(self.case.case_id) / f"runs/{result.json()['run_id']}.output.json").read_text(encoding="utf-8"))
        call = next(c for c in audit["tool_calls"] if c["name"] == "propose_evidence_gap")
        error = call["result"]["error"]["validation_details"]["errors"][0]
        self.assertEqual(error["code"], "duplicate_reference")
        self.assertEqual(error["argument_loc"], ["basis_info_unit_ids"])

    def legacy_workspace(self):
        self.create_gap()
        path = case_dir(self.case.case_id) / "issue_workspaces.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        old_gap = payload["workspaces"][self.issue]["reasoning"]["gaps"][0]
        old_gap["info_unit_ids"] = old_gap.pop("basis_info_unit_ids")
        old_gap.pop("addressing_info_unit_ids")
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        (path.parent / "case.json").write_text(self.case.model_dump_json(), encoding="utf-8")
        return path, payload

    def test_explicit_migration_preserves_every_other_field_and_is_idempotent(self):
        path, before = self.legacy_workspace()
        original = path.read_bytes()
        with self.assertRaises(AppError):
            get_issue_workspaces(self.case)
        self.assertEqual(migrate_workspace(path), 1)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(migrate_workspace(path, apply=True), 1)
        backup = path.with_suffix(path.suffix + ".before-gap-reference-split.bak")
        self.assertEqual(backup.read_bytes(), original)
        expected = deepcopy(before)
        gap = expected["workspaces"][self.issue]["reasoning"]["gaps"][0]
        gap["basis_info_unit_ids"] = gap.pop("info_unit_ids")
        gap["addressing_info_unit_ids"] = []
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), expected)
        saved = path.read_bytes()
        self.assertEqual(migrate_workspace(path, apply=True), 0)
        self.assertEqual(path.read_bytes(), saved)
        self.assertEqual(backup.read_bytes(), original)
        self.assertEqual(self.gap().basis_info_unit_ids, ["U029"])

    def test_migration_validation_or_write_failure_never_overwrites_original(self):
        path, payload = self.legacy_workspace()
        original = path.read_bytes()
        with patch("scripts.migrate_gap_references.write_json", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                migrate_workspace(path, apply=True)
        self.assertEqual(path.read_bytes(), original)
        gap = payload["workspaces"][self.issue]["reasoning"]["gaps"][0]
        gap["basis_info_unit_ids"] = []
        path.write_text(json.dumps(payload), encoding="utf-8")
        mixed = path.read_bytes()
        with self.assertRaisesRegex(ValueError, "Mixed"):
            migrate_workspace(path, apply=True)
        self.assertEqual(path.read_bytes(), mixed)
