"""InfoUnit-only evidence authority, source integrity and historical run compatibility."""
import json
import unittest
from unittest.mock import patch

import test_reasoning_proposals as fixture
import test_investigation_agent as replies
from app import investigation_actions as actions, investigation_agent as agent, reasoning_proposals
from app.case_store import case_dir, artifact_path
from app.errors import AppError
from app.issue_workspaces import get_issue_workspaces, investigation_context


class TestProposalInfoUnitEvidence(unittest.TestCase):
    setUp = fixture.TestReasoningProposals.setUp
    tearDown = fixture.TestReasoningProposals.tearDown
    edit = fixture.TestReasoningProposals.edit
    hypothesis = fixture.TestReasoningProposals.hypothesis
    run_script = replies.TestInvestigationAgent.run_script

    def state(self):
        return get_issue_workspaces(self.case).workspaces[self.issue].reasoning

    def test_all_five_proposals_commit_with_summary_evidence_and_same_run_dependencies(self):
        hid = self.hypothesis("既有命题：指定期间存在实际租金断档")
        gid = self.edit(gap={"content": "既有问题：实际收款金额是多少？", "hypothesis_ids": [hid],
                            "basis_info_unit_ids": ["U026"]}).json()["object_id"]
        def respond(call, turn):
            view = json.loads(call["messages"][1]["content"])
            self.assertEqual(set(view), {"goal", "current_issue", "materials", "reasoning", "proposals", "notices", "recovery"})
            self.assertNotIn("core_issue", view)
            self.assertNotIn("case_info_directory", view)
            if turn == 1:
                return replies.tool("search_info_units", {"query": "损失"}, "summary")
            if turn == 2:
                self.assertIn("U029", {row["id"] for row in view["materials"]["info_units"]["items"]})
                return replies.tool("propose_hypothesis", {"content": "替租安排可能避免了租金断档", "info_unit_ids": ["U029"],
                    "rationale": "当事人陈述可形成待核查命题，尚不能确证。"}, "hyp")
            parent = view["proposals"][0]["proposal_id"]
            if turn == 3:
                return replies.tool("propose_evidence_link", {"hypothesis_id": None,
                    "hypothesis_proposal_ids": [parent], "info_unit_id": "U029", "meaning": "related",
                    "rationale": "该陈述与实际空置情况有关。"}, "link")
            if turn == 4:
                return replies.tool("propose_evidence_gap", {"content": "替租期间房屋是否持续实际占用？",
                    "hypothesis_targets": {"hypothesis_ids": [], "hypothesis_proposal_ids": [parent]},
                    "basis_info_unit_ids": ["U029"], "rationale": "单方陈述仍需实际占用记录回应。"}, "gap")
            if turn == 5:
                return replies.tool("propose_hypothesis_status", {"hypothesis_id": hid, "judgment": "verified",
                    "info_unit_ids": ["U029"], "rationale": "离线传输契约样例，不验证证据判断质量。"}, "status")
            if turn == 6:
                return replies.tool("propose_evidence_gap_update", {"gap_id": gid, "status": "open",
                    "addressing_info_unit_ids": ["U029"], "rationale": "新增回应仍未充分解释金额。"}, "update")
            return replies.finish("围绕实收情况形成推理", status="completed", questions=["后续可核查其他期间。"])
        with patch.object(reasoning_proposals, "persist_issue_workspaces", wraps=reasoning_proposals.persist_issue_workspaces) as save:
            result, calls = self.run_script("围绕实收情况形成推理", respond, issue_key=self.issue,
                work_context=investigation_context(self.case, self.issue, selected_reasoning_ids=[gid]))
        self.assertEqual(result["workspace_commit"]["status"], "applied", result)
        self.assertEqual(save.call_count, 1)
        self.assertEqual(len(result["proposals"]), 5)
        self.assertTrue(all("evidence_refs" not in p for p in result["proposals"]))
        self.assertFalse(result["evidence_refs"])
        record = self.state().investigation_runs[result["run_id"]]
        self.assertFalse(record.evidence)
        created = next(h.id for h in self.state().hypotheses if h.id != hid)
        self.assertEqual(self.state().links[0].hypothesis_id, created)
        self.assertEqual(next(g for g in self.state().gaps if g.id != gid).hypothesis_ids, [created])
        self.assertEqual(next(g for g in self.state().gaps if g.id == gid).addressing_info_unit_ids, ["U029"])

    def test_initial_selected_summary_grants_material_authority_without_reading_sources(self):
        result, calls = self.run_script("形成待核查解释", [
            replies.tool("propose_hypothesis", {"content": "收款主张可能需要独立核实", "info_unit_ids": ["U029"],
                "rationale": "依据明确选中的材料摘要。"}, "hyp"), replies.finish("形成待核查解释", status="completed")],
            selected=["U029"], issue_key=self.issue)
        self.assertEqual(result["workspace_commit"]["status"], "applied")
        self.assertEqual(result["tool_budget_used"]["read"], 0)
        for call in calls:
            view = json.loads(call["messages"][1]["content"])
            self.assertNotIn("core_issue", view)
            self.assertNotIn("case_info_directory", view)

    def test_unobtained_and_nonexistent_material_are_rejected_before_staging(self):
        context = investigation_context(self.case, self.issue)
        for uid, code in (("U029", "reference_not_obtained"), ("foreign", "reference_not_found")):
            pending = []
            with self.subTest(uid=uid), self.assertRaises(AppError) as caught:
                actions.propose_hypothesis(self.case, {"units": set(), "relations": set(), "evidence": set()},
                    context, pending, content="独立待核实命题", rationale="需实际取得材料。", info_unit_ids=[uid])
            self.assertFalse(pending)
            self.assertEqual(caught.exception.details["errors"][0]["code"], code)

    def test_relation_ids_alone_do_not_grant_unreturned_info_unit_content(self):
        from app import investigation_tools
        result = investigation_tools.get_relations(self.case, ["U029"])
        self.assertTrue(result["items"])
        seen = {"units": set(), "relations": set(), "evidence": set()}
        agent._remember("get_relations", result, seen)
        self.assertNotIn("U029", seen["units"])
        self.assertEqual(seen["units"], {unit["id"] for item in result["items"] for unit in item["adjacent_info_units"]})

    def test_summary_only_proposal_still_checks_unread_source_integrity_before_commit(self):
        ref = next(u for u in self.case.info_units if u.id == "U029").source_refs[1]
        file = next(f for f in self.case.files if f.file_id == ref.file_id)
        path = artifact_path(self.case.case_id, file.parsed_text_path)
        original = path.read_bytes()
        def respond(call, turn):
            if turn == 1:
                return replies.tool("propose_hypothesis", {"content": "材料记载仍待核实", "info_unit_ids": ["U029"],
                    "rationale": "基于摘要提出可检验命题。"}, "hyp")
            path.write_bytes(b"cache changed without a get_evidence call")
            return replies.finish("核查陈述", status="completed")
        try:
            with patch.object(reasoning_proposals, "persist_issue_workspaces") as save:
                result, _ = self.run_script("核查陈述", respond, selected=["U029"], issue_key=self.issue)
            self.assertEqual(result["workspace_commit"]["status"], "conflict")
            save.assert_not_called()
            self.assertEqual(self.state().hypotheses, [])
        finally:
            path.write_bytes(original)

    def test_historical_proposal_pointers_read_without_rewriting_and_undo_still_works(self):
        result, _ = self.run_script("提出解释", [replies.tool("propose_hypothesis", {
            "content": "单方主张仍需核实", "info_unit_ids": ["U029"], "rationale": "保留不确定性。"}, "hyp"),
            replies.finish("提出解释", status="completed")], selected=["U029"], issue_key=self.issue)
        path = case_dir(self.case.case_id) / "issue_workspaces.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["workspaces"][self.issue]["reasoning"]["investigation_runs"][result["run_id"]]["proposals"][0]["evidence_refs"] = [
            {"info_unit_id": "U029", "source_ref_index": 0}]
        path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        saved = path.read_bytes()
        self.assertEqual(len(self.state().investigation_runs[result["run_id"]].proposals), 1)
        self.assertEqual(path.read_bytes(), saved)
        url = f"/api/cases/{self.case.case_id}/investigation-changes/reverse"
        for restore in (False, True):
            response = self.client.post(url, json={"issue": self.issue, "run_id": result["run_id"], "restore": restore})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(len(self.state().hypotheses), int(restore))
            current = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(current["workspaces"][self.issue]["reasoning"]["investigation_runs"][result["run_id"]]["proposals"],
                             raw["workspaces"][self.issue]["reasoning"]["investigation_runs"][result["run_id"]]["proposals"])
        following, _ = self.run_script("后续独立核查", [replies.tool("propose_hypothesis", {
            "content": "合同约定可能与实际履行不同", "info_unit_ids": ["U029"], "rationale": "独立待核实命题。"}, "next-hyp"),
            replies.finish("后续独立核查", status="completed")], selected=["U029"], issue_key=self.issue)
        self.assertEqual(following["workspace_commit"]["status"], "applied", following)
        current = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(current["workspaces"][self.issue]["reasoning"]["investigation_runs"][result["run_id"]]["proposals"],
                         raw["workspaces"][self.issue]["reasoning"]["investigation_runs"][result["run_id"]]["proposals"])
