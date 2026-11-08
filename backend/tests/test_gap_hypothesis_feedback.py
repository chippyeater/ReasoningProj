"""Case A2 regressions: last execution and Gap-Hypothesis feedback."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

import test_investigation_agent as f
import test_reasoning_proposals as fixture
from app import investigation_actions as actions, investigation_state as state
from app.errors import AppError
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
        self.assertEqual((previous["execution_status"], previous["effect_status"]), ("succeeded", "applied"))
        related = previous["result"]["next_step"]["related_hypotheses"]
        self.assertEqual([h["hypothesis_id"] for h in related], self.hids)
        self.assertEqual([h["content"] for h in related], [h.content for h in before.hypotheses])
        self.assertTrue(all(h["judgment"] == "unverified" for h in related))
        self.assertNotIn("effective_reasoning", view)
        self.assertEqual(view["reasoning"]["gaps"][0]["status"], "resolved")
        final = get_issue_workspaces(self.case).workspaces[self.issue].reasoning
        self.assertEqual([h.judgment for h in final.hypotheses], ["unverified", "unverified"])
        self.assertEqual([p["kind"] for p in result["proposals"]], ["evidence_gap_update"])
        self.assertEqual(result["completion_status"], "completed")

    def test_pending_judgment_is_filtered_and_no_change_retains_full_feedback(self):
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
        related = previous["result"]["next_step"]["related_hypotheses"]
        self.assertEqual([h["hypothesis_id"] for h in related], self.hids[1:])
        self.assertEqual(related[0]["judgment"], "unverified")
        view = self.view(calls[3])
        self.assertEqual({h["id"] for h in view["reasoning"]["hypotheses"]}, set(self.hids))
        self.assertEqual(next(h["judgment"] for h in view["reasoning"]["hypotheses"] if h["id"] == self.hids[0]), "verified")
        self.assertEqual(view["proposals"][1]["hypothesis_ids"], self.hids[:1])
        self.assertEqual(view["proposals"][1]["judgment"], "verified")
        self.assertEqual(self.view(calls[4])["previous_tool_result"]["effect_status"], "no_change")
        self.assertEqual(len(result["proposals"]), 2)
        self.assertEqual(get_issue_workspaces(self.case).workspaces[self.issue].reasoning.hypotheses[0].judgment, "verified")

    def test_all_pending_judgments_leave_an_empty_next_step_without_changing_state(self):
        judgments = ("verified", "refuted")
        updates = [f.tool("propose_hypothesis_status", {"hypothesis_id": hid, "judgment": judgment,
            "info_unit_ids": ["U029"], "rationale": "离线协议回归：已成功暂存的判断。"}, f"hyp-{i}")
            for i, (hid, judgment) in enumerate(zip(self.hids, judgments))]
        result, calls = self.run_script("回应 Gap", [*updates,
            f.tool("propose_evidence_gap_update", self.update, "gap"), f.finish("回应 Gap", status="completed")],
            selected=["U029"], issue_key=self.issue, work_context=self.context)
        view = self.view(calls[3])
        next_step = view["previous_tool_result"]["result"]["next_step"]
        self.assertEqual(next_step["related_hypotheses"], [])
        self.assertIn("无需继续提交", next_step["instruction"])
        self.assertEqual({h["id"]: h["judgment"] for h in view["reasoning"]["hypotheses"]}, dict(zip(self.hids, judgments)))
        self.assertEqual([p["kind"] for p in result["proposals"]],
            ["hypothesis_status", "hypothesis_status", "evidence_gap_update"])
        self.assertEqual([h.judgment for h in get_issue_workspaces(self.case).workspaces[self.issue].reasoning.hypotheses], list(judgments))

    def test_invalid_and_no_change_status_calls_do_not_filter_hypotheses(self):
        for overrides, execution_status in [({"judgment": "invalid"}, "failed"),
                ({"info_unit_ids": ["foreign-unit"]}, "failed"), ({"judgment": "unverified"}, "succeeded")]:
            with self.subTest(overrides=overrides):
                args = {"hypothesis_id": self.hids[0], "judgment": "verified", "info_unit_ids": ["U029"],
                    "rationale": "此次操作未成功暂存。", **overrides}
                result, calls = self.run_script("回应 Gap", [f.tool("propose_hypothesis_status", args, "status"),
                    f.tool("propose_evidence_gap_update", self.update, "gap"), f.finish("回应 Gap", status="completed")],
                    selected=["U029"], issue_key=self.issue, work_context=self.context)
                self.assertEqual(self.view(calls[1])["previous_tool_result"]["execution_status"], execution_status)
                related = self.view(calls[2])["previous_tool_result"]["result"]["next_step"]["related_hypotheses"]
                self.assertEqual([h["hypothesis_id"] for h in related], self.hids)
                self.assertFalse(any(p["kind"] == "hypothesis_status" for p in result["proposals"]))

    def test_rejected_staging_does_not_filter_hypotheses(self):
        from app import reasoning_proposals
        original = reasoning_proposals.apply_proposal
        def apply(case, workspace, proposals, proposal):
            if proposal.kind == "hypothesis_status":
                raise AppError("staging_rejected", "Status update could not be staged")
            return original(case, workspace, proposals, proposal)
        with patch.object(reasoning_proposals, "apply_proposal", side_effect=apply):
            result, calls = self.run_script("回应 Gap", [f.tool("propose_hypothesis_status", {
                "hypothesis_id": self.hids[0], "judgment": "verified", "info_unit_ids": ["U029"],
                "rationale": "模拟暂存失败。"}, "status"), f.tool("propose_evidence_gap_update", self.update, "gap"),
                f.finish("回应 Gap", status="completed")], selected=["U029"], issue_key=self.issue, work_context=self.context)
        self.assertEqual(self.view(calls[1])["previous_tool_result"]["recovery"]["code"], "staging_rejected")
        related = self.view(calls[2])["previous_tool_result"]["result"]["next_step"]["related_hypotheses"]
        self.assertEqual([h["hypothesis_id"] for h in related], self.hids)
        self.assertEqual([p["kind"] for p in result["proposals"]], ["evidence_gap_update"])

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
                return f.tool("propose_hypothesis_status", {"hypothesis_id": self.hids[0],
                    "judgment": "verified", "info_unit_ids": ["U029"], "rationale": "H1 已有成功暂存的判断。"}, "h1")
            if step == 2:
                return f.tool("propose_evidence_gap_update", self.update, "gap")
            if step == 3:
                related = view["previous_tool_result"]["result"]["next_step"]["related_hypotheses"]
                returned_ids.extend(h["hypothesis_id"] for h in related)
                return f.tool("propose_hypothesis_status", {"hypothesis_id": returned_ids[0],
                    "judgment": "refuted", "info_unit_ids": ["U029"], "rationale": "继续处理 H2 的状态更新。"}, "h2")
            return f.finish("核查", status="completed")
        result, _ = self.run_script("核查 Gap 后评估关联对象", respond,
            selected=["U029"], issue_key=self.issue, work_context=self.context)
        self.assertEqual(returned_ids, self.hids[1:])
        self.assertEqual(result["completion_status"], "completed")
        self.assertEqual([h.judgment for h in get_issue_workspaces(self.case).workspaces[self.issue].reasoning.hypotheses],
            ["verified", "refuted"])

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
        from types import SimpleNamespace
        workspace = SimpleNamespace(reasoning=SimpleNamespace(
            gaps=[SimpleNamespace(id="gap-empty", hypothesis_ids=[])], hypotheses=[]))
        with patch.object(actions, "_propose", return_value={"status": "ok", "applied": False, "no_change": True}):
            response = actions.propose_evidence_gap_update(self.case, {}, workspace, [],
                gap_id="gap-empty", status="open", addressing_info_unit_ids=[], rationale="未改变。")
        self.assertEqual(response["next_step"]["related_hypotheses"], [])
