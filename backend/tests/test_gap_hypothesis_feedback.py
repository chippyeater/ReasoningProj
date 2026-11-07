"""Case A2 regressions: last execution and Gap-Hypothesis feedback."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

import test_investigation_agent as f
import test_reasoning_proposals as fixture
from app import investigation_actions as actions, investigation_state as state
from app.issue_workspaces import get_issue_workspaces, investigation_context


class TestGapHypothesisFeedback(unittest.TestCase):
    tearDown = fixture.TestReasoningProposals.tearDown

    edit = fixture.TestReasoningProposals.edit

    hypothesis = fixture.TestReasoningProposals.hypothesis

    run_script = f.TestInvestigationAgent.run_script

    audits = f.TestInvestigationAgent.audits

    def setUp(self):
        fixture.TestReasoningProposals.setUp(self)
        self.hids = [self.hypothesis(), self.hypothesis("替租人已接续支付租金。")]
        self.gid = self.edit(gap={"content": "指定期间是否存在空置租金损失？",
            "hypothesis_ids": self.hids}).json()["object_id"]
        self.context = investigation_context(self.case, self.issue, selected_reasoning_ids=[self.gid])
        self.update = {"gap_id": self.gid, "status": "resolved", "addressing_info_unit_ids": ["U029"],
                       "rationale": "本轮取得材料回应了指定期间问题，判断仍需分别评估。"}

    def run_gap(self, following, **kwargs):
        return self.run_script("核查 Gap 并分别重新评估两条 Hypothesis", [
            f.tool("propose_evidence_gap_update", self.update, "gap-update"), *following],
            selected=["U029"], issue_key=self.issue, work_context=self.context, **kwargs)

    def view(self, call):
        return json.loads(call["messages"][1]["content"])

    def test_gap_update_keeps_hypothesis_judgments_without_status_proposals(self):
        before = get_issue_workspaces(self.case).workspaces[self.issue].reasoning.model_copy(deep=True)
        result, calls = self.run_gap([f.finish("核查", status="completed")])
        view = self.view(calls[1]); previous = view["previous_tool_result"]
        self.assertNotIn("notices", view); self.assertNotIn("recovery", view)
        self.assertEqual(previous["tool_call_id"], "gap-update")
        self.assertEqual(previous["arguments"], self.update)
        self.assertEqual((previous["execution_status"], previous["effect_status"]), ("succeeded", "staged"))
        related = previous["result"]["next_step"]["related_hypotheses"]
        self.assertEqual([h["hypothesis_id"] for h in related], self.hids)
        self.assertEqual([h["content"] for h in related], [h.content for h in before.hypotheses])
        self.assertTrue(all(h["judgment"] == h["formal_judgment"] == h["effective_judgment"] == "unverified" for h in related))
        self.assertEqual(view["reasoning"]["gaps"][0]["status"], "open")
        self.assertEqual(view["effective_reasoning"]["gaps"][0]["status"], "resolved")
        final = get_issue_workspaces(self.case).workspaces[self.issue].reasoning
        self.assertEqual([h.judgment for h in final.hypotheses], ["unverified", "unverified"])
        self.assertEqual([p["kind"] for p in result["proposals"]], ["evidence_gap_update"])
        self.assertEqual(result["completion_status"], "completed")

    def test_pending_judgment_is_returned_and_no_change_retains_full_feedback(self):
        status = {"hypothesis_id": self.hids[0], "judgment": "verified", "info_unit_ids": ["U029"],
                  "rationale": "根据本轮材料更新判断。"}
        result, calls = self.run_gap([f.tool("propose_hypothesis_status", status, "hyp-update"),
            f.tool("propose_evidence_gap_update", {**self.update, "rationale": "同一操作的新措辞。"}, "gap-repeat"),
            f.tool("propose_hypothesis_status", {**status, "rationale": "改写理由不产生新判断。"}, "hyp-repeat"),
            f.finish("核查", status="completed")])
        previous = self.view(calls[3])["previous_tool_result"]
        self.assertEqual(previous["effect_status"], "no_change")
        self.assertTrue(previous["result"]["no_change"])
        self.assertTrue(previous["recovery"]["no_change"])
        related = previous["result"]["next_step"]["related_hypotheses"][0]
        self.assertEqual(related["formal_judgment"], "unverified")
        self.assertEqual(related["effective_judgment"], "verified")
        self.assertEqual(related["pending_changes"][0]["judgment"], "verified")
        self.assertEqual(self.view(calls[4])["previous_tool_result"]["effect_status"], "no_change")
        self.assertEqual(len(result["proposals"]), 2)
        self.assertEqual(get_issue_workspaces(self.case).workspaces[self.issue].reasoning.hypotheses[0].judgment, "verified")

    def test_case_a2_changed_judgments_require_two_real_status_calls_and_commit(self):
        calls_to_make = [f.tool("propose_hypothesis_status", {"hypothesis_id": hid, "judgment": judgment,
            "info_unit_ids": ["U029"], "rationale": "离线协议回归：分别提交两条实际判断操作。"}, f"status-{index}")
            for index, (hid, judgment) in enumerate(zip(self.hids, ("verified", "refuted")))]
        result, calls = self.run_gap([*calls_to_make, f.finish("核查", status="completed")])
        final = get_issue_workspaces(self.case).workspaces[self.issue].reasoning
        self.assertEqual([h.judgment for h in final.hypotheses], ["verified", "refuted"])
        self.assertEqual([p["kind"] for p in result["proposals"]],
                         ["evidence_gap_update", "hypothesis_status", "hypothesis_status"])
        self.assertEqual(result["workspace_commit"]["status"], "applied")

    def test_offline_agent_follows_gap_returned_targets_before_finish(self):
        returned_ids = []
        def respond(call, step):
            view = self.view(call)
            if step == 1:
                return f.tool("propose_evidence_gap_update", self.update, "gap")
            if step == 2:
                related = view["previous_tool_result"]["result"]["next_step"]["related_hypotheses"]
                returned_ids.extend(h["hypothesis_id"] for h in related)
                return f.tool("propose_hypothesis_status", {"hypothesis_id": returned_ids[0],
                    "judgment": "verified", "info_unit_ids": ["U029"], "rationale": "离线协议回归的状态更新。"}, "status")
            return f.finish("核查", status="completed")
        result, _ = self.run_script("核查 Gap 后评估关联对象", respond,
            selected=["U029"], issue_key=self.issue, work_context=self.context)
        self.assertEqual(returned_ids, self.hids)
        self.assertEqual(result["completion_status"], "completed")

    def test_last_execution_replaces_truncation_and_failure_without_mutating_originals(self):
        first = {"status": "truncated", "items": [], "has_more": True, "error": None}
        raw = '{"query":"租金","limit":1}'
        previous = state.previous_tool_result("search_info_units", "read-1", raw, first, json.loads(raw))
        self.assertEqual(previous["effect_status"], "read")
        self.assertEqual(previous["raw_arguments"], raw)
        self.assertEqual(previous["result"], first)
        original = deepcopy(previous)
        memory = state.KnowledgeState({"goal": "核查"})
        memory.previous_tool_result = previous
        failure = {"status": "error", "items": [], "has_more": False,
                   "error": {"code": "read_failed", "message": "Failed to read"}}
        memory.previous_tool_result = state.previous_tool_result("get_evidence", "read-2", '{"info_unit_id":"U029"}', failure, {"info_unit_id": "U029"})
        view = state.model_context({"goal": "核查"}, memory)
        self.assertEqual(view["previous_tool_result"]["effect_status"], "failed")
        self.assertEqual(view["previous_tool_result"]["notices"], [])
        self.assertNotIn("truncated", json.dumps(view))
        self.assertEqual(previous, original)
        self.assertEqual(first["status"], "truncated")

    def test_no_related_hypotheses_returns_empty_and_never_invents_a_target(self):
        with patch.object(actions, "_propose", return_value={"status": "ok", "pending": False, "no_change": True}):
            from types import SimpleNamespace
            workspace = SimpleNamespace(reasoning=SimpleNamespace(gaps=[SimpleNamespace(id="gap-empty", hypothesis_ids=[])]))
            with patch("app.issue_workspaces.get_issue_workspaces", return_value=SimpleNamespace(workspaces={self.issue: workspace})), \
                    patch.object(actions, "_hypothesis_states", return_value=({}, {})):
                response = actions.propose_evidence_gap_update(self.case, {}, {"current_issue": self.issue}, [],
                    gap_id="gap-empty", status="open", addressing_info_unit_ids=[], rationale="未改变。")
        self.assertEqual(response["next_step"]["related_hypotheses"], [])
