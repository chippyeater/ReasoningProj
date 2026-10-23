"""Real HTTP/model payload boundary with mock completions, user-only adoption."""
import json
from unittest.mock import patch
import httpx

import test_investigation_agent as replies
from test_reasoning_workspace import TestReasoningWorkspace
from app import llm_client
from app.case_store import case_dir
from app.issue_workspaces import get_issue_workspaces, investigation_context


def suggestion(kind, hid=None):
    return {"kind": kind, "content": "房东是否存在可核实的空置损失？" if kind in {"hypothesis", "evidence_gap"} else None,
            "hypothesis_ids": [hid] if hid else [], "info_unit_ids": ["U029"],
            "meaning": "oppose" if kind == "evidence_link" else None,
            "judgment": "unverified" if kind == "hypothesis_status" else None,
            "rationale": "张某陈述无损失，仍应核对入住和收租材料。",
            "evidence_refs": [{"info_unit_id": "U029", "source_ref_index": 0}]}


class TestReasoningProposals(TestReasoningWorkspace):
    # Do not repeat parent tests in this suite.
    test_edit_link_gap_and_restore_without_business_copies = None
    test_invalid_refs_duplicates_and_write_failure_leave_file_unchanged = None
    test_legacy_workspace_and_referenced_evidence_are_protected = None

    def investigate(self, proposals, selected=None):
        goal = "检查假设的支持、反对依据及证据缺口"
        final = replies.final(goal, ["U029"], refs=[("U029", 0)], status="completed")
        content = json.loads(final.content)
        if proposals is not None:
            content["proposals"] = proposals
        final.message["content"] = final.content = json.dumps(content, ensure_ascii=False)
        final.response["choices"][0]["message"] = final.message
        script = [replies.tool("get_evidence", {"info_unit_ids": ["U029"]}), final]
        requests = []
        real_client = httpx.AsyncClient

        def model(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json=script[len(requests)-1].response)

        with patch.object(llm_client.httpx, "AsyncClient", side_effect=lambda **kwargs:
                          real_client(transport=httpx.MockTransport(model), **kwargs)):
            response = self.client.post(f"/api/cases/{self.case.case_id}/agent/run", json={
                "goal": goal, "current_issue": self.issue, "selected_reasoning_ids": selected or []})
        return response, requests

    def accept(self, run_id, index=0, issue=None):
        return self.client.post(f"/api/cases/{self.case.case_id}/reasoning-proposals/accept", json={
            "issue": issue or self.issue, "run_id": run_id, "proposal_index": index, "position": {"x": 1500, "y": 500}})

    def test_validated_proposals_remain_preview_until_atomic_idempotent_adoption(self):
        hid = self.hypothesis()
        path = case_dir(self.case.case_id) / "issue_workspaces.json"
        before = path.read_bytes()
        response, requests = self.investigate([suggestion("hypothesis"), suggestion("evidence_link", hid),
                                                suggestion("evidence_gap", hid), suggestion("hypothesis_status", hid)], [hid])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(path.read_bytes(), before)
        initial = json.loads(requests[0]["messages"][1]["content"])
        self.assertEqual(initial["work_context"]["reasoning"]["hypotheses"][0]["id"], hid)
        self.assertNotIn("source_refs", json.dumps(initial))
        self.assertNotIn("node_positions", json.dumps(initial))
        self.assertNotIn("visible_node_ids", json.dumps(initial))
        self.assertIn("proposals", requests[0]["response_format"]["json_schema"]["schema"]["required"])
        run_id = response.json()["run_id"]
        self.assertEqual(self.accept(run_id, issue=self.case.core_issue).status_code, 409)
        accepted = self.accept(run_id)
        self.assertEqual(accepted.status_code, 200, accepted.text)
        obj = accepted.json()["object_id"]
        again = self.accept(run_id)
        self.assertTrue(again.json()["already_accepted"])
        self.assertEqual(again.json()["object_id"], obj)
        self.assertEqual(len(get_issue_workspaces(self.case).workspaces[self.issue].reasoning.hypotheses), 2)
        # Link adoption is independent of new Hypothesis adoption; source remains shared.
        linked = self.accept(run_id, 1)
        self.assertEqual(linked.status_code, 200, linked.text)
        link = get_issue_workspaces(self.case).workspaces[self.issue].reasoning.links[0]
        self.assertEqual((link.origin, link.meaning, link.info_unit_id), ("agent", "oppose", "U029"))
        # Later proposals based on the old target must not overwrite subsequent edits.
        self.assertEqual(self.accept(run_id, 3).status_code, 409)

    def test_user_corrections_and_evidence_changes_invalidate_old_suggestions(self):
        hid = self.hypothesis()
        response, _ = self.investigate([suggestion("hypothesis_status", hid)], [hid])
        self.assertEqual(response.status_code, 200, response.text)
        self.edit(hypothesis={"id": hid, "content": "用户修正：不能将陈述当作核实", "judgment": "refuted", "work_status": "deprecated"})
        self.assertEqual(self.accept(response.json()["run_id"]).status_code, 409)
        context = investigation_context(self.case, self.issue, "检查当前假设", [hid])
        current = context["reasoning"]["hypotheses"][0]
        self.assertEqual((current["judgment"], current["work_status"], current["last_edited_by"]), ("refuted", "deprecated", "user"))
        self.assertIn("用户修正", current["content"])
        next_run, requests = self.investigate(None, [hid])
        self.assertEqual(next_run.status_code, 200, next_run.text)
        wire = json.loads(requests[0]["messages"][1]["content"])["work_context"]["reasoning"]["hypotheses"][0]
        self.assertEqual((wire["judgment"], wire["work_status"], wire["revision"]),
                         (current["judgment"], current["work_status"], current["revision"]))
        self.assertEqual(wire["content"], current["content"])
        fresh, _ = self.investigate([suggestion("hypothesis_status", hid)], [hid])
        self.assertEqual(fresh.status_code, 200, fresh.text)
        self.assertEqual(self.accept(fresh.json()["run_id"]).status_code, 200)
        adopted = get_issue_workspaces(self.case).workspaces[self.issue].reasoning.hypotheses[0]
        self.assertEqual((adopted.judgment, adopted.work_status, adopted.content),
                         ("unverified", "deprecated", current["content"]))
        response, _ = self.investigate([suggestion("hypothesis")])
        unit = next(u for u in self.case.info_units if u.id == "U029")
        old = unit.content
        unit.content = "原材料的描述已由用户修正，需要重新调查。"
        self.assertEqual(self.accept(response.json()["run_id"]).status_code, 409)
        unit.content = old

    def test_invalid_unretrieved_evidence_unknown_targets_and_missing_suggestions(self):
        hid = self.hypothesis()
        for bad in ("unretrieved", "unknown_target", "bad_ref"):
            proposal = suggestion("evidence_link", hid)
            if bad == "unretrieved":
                proposal["info_unit_ids"] = ["U026"]
            elif bad == "unknown_target":
                proposal["hypothesis_ids"] = ["foreign"]
            else:
                proposal["evidence_refs"][0]["source_ref_index"] = 9
            response, _ = self.investigate([proposal])
            self.assertEqual(response.status_code, 422, response.text)
        response, _ = self.investigate(None)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["proposals"], [])
        self.assertEqual(self.accept(response.json()["run_id"]).status_code, 409)

    def test_save_failure_has_no_partial_adoption_and_can_retry(self):
        hid = self.hypothesis()
        response, _ = self.investigate([suggestion("evidence_gap", hid)])
        self.assertEqual(response.status_code, 200, response.text)
        path = case_dir(self.case.case_id) / "issue_workspaces.json"
        before = path.read_bytes()
        with patch("app.issue_workspaces.write_json", side_effect=OSError("full")):
            self.assertEqual(self.accept(response.json()["run_id"]).status_code, 500)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.accept(response.json()["run_id"]).status_code, 200)
        state = get_issue_workspaces(self.case).workspaces[self.issue].reasoning
        self.assertEqual(len(state.gaps), 1)
        self.assertEqual(state.gaps[0].status, "open")

    def test_context_is_bounded_and_prioritizes_explicit_selection(self):
        ids = [self.hypothesis(f"待核实的备选假设 {i}") for i in range(8)]
        context = investigation_context(self.case, self.issue, "检查假设", [ids[0]])["reasoning"]
        self.assertLessEqual(len(context["hypotheses"]), 3)
        self.assertEqual(context["hypotheses"][0]["id"], ids[0])
        self.assertTrue(context["has_more"])
