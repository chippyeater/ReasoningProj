"""Bounded error recovery and explicit source delivery with Case A mocks."""
import json
import unittest
from unittest.mock import patch

import test_investigation_agent as fixture
from test_reasoning_proposals import suggestion, proposal_call
from app import investigation_agent as agent, investigation_tools as tools
from app.errors import AppError


class TestInvestigationRecovery(unittest.TestCase):
    setUp = fixture.TestInvestigationAgent.setUp
    tearDown = fixture.TestInvestigationAgent.tearDown
    run_script = fixture.TestInvestigationAgent.run_script
    audits = fixture.TestInvestigationAgent.audits

    def test_failed_proposal_can_retry_after_fetching_the_missing_source(self):
        proposal = suggestion("hypothesis")
        result, calls = self.run_script("核查损失并建立假设", [proposal_call(proposal, [], "missing"),
            fixture.tool("get_evidence", {"info_unit_ids": ["U029"]}, "read"), proposal_call(proposal, [], "corrected"),
            fixture.present("核查损失并建立假设", ["U029"], refs=[("U029", 0)]), fixture.final("现有陈述不足以证实损失，已提出待检验假设，可查看后决定采纳。")],
            work_context={"reasoning": {"hypotheses": [], "gaps": []}}, issue_key=self.case.issue_set[1])
        error = json.loads(calls[1]["messages"][-1]["content"])["error"]
        self.assertEqual(error["validation_details"]["errors"][0]["code"], "reference_not_obtained")
        self.assertEqual(error["validation_details"]["errors"][0]["argument_loc"], ["info_unit_ids", 0])
        self.assertIn("get_evidence", error["recovery_hint"])
        self.assertEqual(len(result["proposals"]), 1)
        self.assertEqual(result["tool_attempt_count"], 4)
        self.assertEqual(result["summary_status"], "completed")

    def test_duplicate_call_is_blocked_but_a_different_action_can_continue(self):
        original = tools.get_evidence
        with patch.object(tools, "get_evidence", wraps=original) as reader:
            result, _ = self.run_script("核对并扩展", [fixture.tool("get_evidence", {"info_unit_ids": ["U029"]}),
                fixture.tool("get_evidence", {"info_unit_ids": ["U029"]}, "duplicate"),
                fixture.tool("get_relations", {"info_unit_ids": ["U029"]}, "relations"),
                fixture.present("核对并扩展", ["U029"], refs=[("U029", 0)]), fixture.final("已核对陈述及相关分歧，仍需独立材料。")])
        self.assertEqual(reader.call_count, 1)
        self.assertEqual(result["stop_reason"], "model_finished")
        self.assertEqual(self.audits()[0]["tool_calls"][1]["result"]["error"]["code"], "duplicate_query")

    def test_repeated_errors_stop_after_three_attempts_without_executing_again(self):
        result, calls = self.run_script("核对来源", [
            fixture.tool("get_evidence", {"info_unit_ids": ["foreign"]}, str(i)) for i in range(3)] +
            [fixture.final("该来源不可用，暂不能判断；需要提供有效材料。")])
        self.assertEqual(result["stop_reason"], "no_progress_limit")
        self.assertEqual(result["tool_call_count"], 1)
        self.assertEqual(result["tool_attempt_count"], 3)
        self.assertEqual(calls[-1]["tool_choice"], "none")

    def test_fact_answer_must_deliver_its_available_initial_source(self):
        answer = "据张某陈述，替租人于 3 月 8 日搬入，尚未独立核实。"
        result, calls = self.run_script("什么时候搬入", [fixture.final(answer),
            fixture.present("什么时候搬入", ["U026"], refs=[("U026", 0)], statement=answer), fixture.final(answer)], selected=["U026"])
        self.assertEqual(len(calls), 3)
        self.assertEqual(result["tool_budget_used"]["read"], 0)
        self.assertEqual(result["evidence_refs"], [{"info_unit_id": "U026", "source_ref_index": 0}])
        self.assertEqual(self.audits()[0]["model_calls"][0]["completion_check"]["missing_source_delivery"], True)

    def test_insufficient_evidence_can_be_explicitly_delivered_without_fabrication(self):
        from app.investigation_actions import present_findings
        with self.assertRaises(AppError) as caught:
            present_findings(self.case, {"units": set(), "relations": set(), "evidence": set()}, None,
                "没有发现", [], [], [], findings=[], open_questions=[], completion_status="completed")
        self.assertEqual(caught.exception.details["errors"][0]["code"], "empty_completed_findings")
        result, calls = self.run_script("损失金额是多少", [fixture.final("材料不足以确定金额。"),
            fixture.present("损失金额是多少", questions=["能否提供计租日及实际收款记录？"]), fixture.final("材料不足以确定损失金额，请补充计租日及收款记录。")], selected=["U029"])
        self.assertEqual(len(calls), 3)
        self.assertEqual(result["findings"], [])
        self.assertEqual(result["proposals"], [])
        self.assertEqual(result["evidence_refs"], [])
        self.assertEqual(result["completion_status"], "evidence_insufficient")

    def test_ignored_delivery_check_fails_after_one_reminder(self):
        with self.assertRaises(AppError) as caught:
            self.run_script("什么时候搬入", [fixture.final("已明确搬入日期。"), fixture.final("已明确搬入日期。")], selected=["U026"])
        self.assertEqual(caught.exception.code, "investigation_delivery_incomplete")
        self.assertEqual(len(self.audits()[0]["model_calls"]), 2)
        self.assertEqual(self.audits()[0]["status"], "failed")

    def test_recoverable_failure_gets_one_completion_check_and_can_submit_corrected_proposal(self):
        h = suggestion("hypothesis")
        bad = {**h, "info_unit_ids": ["U029", "U029"]}
        result, calls = self.run_script("核对并提出假设", [proposal_call(bad, [], "bad"), fixture.final("暂未提交建议。"),
            proposal_call(h, [], "corrected"), fixture.present("核对并提出假设", ["U029"], refs=[("U029", 0)]), fixture.final("损失尚未证实，已提出待检验假设，可查看或补充材料。")], selected=["U029"],
            work_context={"reasoning": {"hypotheses": [], "gaps": []}}, issue_key=self.case.issue_set[1])
        self.assertEqual(len(result["proposals"]), 1)
        self.assertIn("完成检查", calls[2]["messages"][-1]["content"])
        error = self.audits()[0]["tool_calls"][0]["result"]["error"]
        self.assertEqual(error["validation_details"]["errors"][0]["code"], "duplicate_reference")
        self.assertIn("info_unit_ids", error["validation_details"]["errors"][0]["argument_loc"])

    def test_reference_failure_categories_and_parameter_locations(self):
        from app.investigation_actions import artifacts
        seen = {"units": {u.id for u in self.case.info_units}, "relations": set(), "evidence": {("U029", 0)}}
        for uid, index, expected in (("foreign", 0, "reference_not_found"), ("U029", 999, "reference_out_of_range"), ("U026", 0, "reference_not_obtained")):
            with self.subTest(expected=expected), self.assertRaises(AppError) as caught:
                artifacts(self.case, seen, None, "引用", [], [{"statement": "待核验", "info_unit_ids": [uid], "relation_ids": [],
                    "evidence_refs": [{"info_unit_id": uid, "source_ref_index": index}]}], [], "evidence_insufficient")
            self.assertEqual(caught.exception.details["errors"][0]["code"], expected)
        with self.assertRaises(AppError) as caught:
            agent._arguments("propose_hypothesis", '{}')
        self.assertEqual(caught.exception.details["category"], "structure")
        self.assertTrue(caught.exception.details["errors"][0]["loc"])
        response = tools._failure(AppError("invalid_investigation_reference", "Unknown Hypothesis"))
        response["error"]["validation_details"] = {"category": "reference", "errors": [{
            "loc": ["proposals", 0, "hypothesis_ids", 0], "code": "reference_not_obtained"}]}
        agent._tool_recovery(response, "propose_evidence_link")
        self.assertEqual(response["error"]["validation_details"]["errors"][0]["code"], "invalid_hypothesis_target")
        self.assertEqual(response["error"]["validation_details"]["errors"][0]["argument_loc"], ["hypothesis_id"])
        self.assertNotIn("get_evidence", response["error"]["recovery_hint"])

    def test_read_after_failed_proposal_does_not_bypass_recovery_review(self):
        good = suggestion("hypothesis")
        bad = {**good, "info_unit_ids": ["U029", "U029"]}
        result, calls = self.run_script("核对并提出假设", [
            fixture.tool("get_evidence", {"info_unit_ids": ["U029"]}),
            fixture.present("核对并提出假设", ["U029"], refs=[("U029", 0)]),
            proposal_call(bad, [], "bad"), fixture.tool("search_info_units", {"query": "计租记录"}, "more"),
            fixture.final("来源已核对，但建议尚未提交。"), proposal_call(good, [], "fixed"),
            fixture.final("相关说法尚未独立核实，已提出待检验假设，可查看后决定采纳。")],
            work_context={"reasoning": {"hypotheses": [], "gaps": []}}, issue_key=self.case.issue_set[1])
        self.assertIn("完成检查", calls[5]["messages"][-1]["content"])
        self.assertEqual(len(result["proposals"]), 1)


if __name__ == "__main__":
    unittest.main()
