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
    run_script = fixture.TestInvestigationAgent.run_execution_script
    audits = fixture.TestInvestigationAgent.audits

    def test_ordinary_text_requires_finish_without_system_reminder(self):
        answer = "据张某陈述，替租人于 3 月 8 日搬入，尚未独立核实。"
        result, calls = self.run_script("搬入", [fixture.final(answer), fixture.finish("搬入", ["U026"], refs=[("U026", 0)], statement=answer)], selected=["U026"])
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1]["tool_choice"], "required")
        self.assertEqual(len(calls[1]["messages"]), 2)
        self.assertEqual(json.loads(calls[1]["messages"][1]["content"])["recovery"]["code"], "finish_required")
        self.assertEqual(result["stop_reason"], "finish_investigation")
        self.assertTrue(all("completion_check" not in c for c in self.audits()[0]["model_calls"]))

    def test_repeated_prose_and_errors_stop_with_unfinished_state(self):
        for script in ([fixture.final("已完成")]*3,
                       [fixture.tool("get_evidence", {"info_unit_ids": ["foreign"]}, str(i)) for i in range(3)]):
            result, calls = self.run_script("核对", script)
            self.assertEqual(result["stop_reason"], "no_progress_limit")
            self.assertEqual(result["summary_status"], "failed")
            self.assertEqual(result["question_state"]["questions"][0]["status"], "pending")
            self.assertEqual(len(calls), 3)
            self.assertNotIn(result["stop_reason"], " ".join(result["limitations"]))

    def test_failed_tool_status_survives_compaction_without_answering_question(self):
        with patch.object(tools, "get_evidence", side_effect=OSError("cache unavailable")):
            result, calls = self.run_script("核对", [fixture.tool("get_evidence", {"info_unit_ids": ["U029"]}),
                fixture.tool("search_info_units", {"query": "不存在的记录"}, "read"),
                fixture.finish("核对", updates=[fixture.question("核对", status="blocked", block_kind="tool_failure", reason="原文缓存读取失败，无法核验来源")])])
        state = json.loads(calls[-1]["messages"][1]["content"])
        self.assertEqual(state["question_state"]["progress"][0]["status"], "pending")
        self.assertEqual(state["knowledge_state"]["activities"]["items"][1]["status"], "error")
        self.assertEqual(result["question_state"]["questions"][0]["block_kind"], "tool_failure")

    def test_model_budget_stops_even_if_valid_reads_interrupt_premature_text(self):
        with patch.object(agent, "MAX_MODEL_CALLS", 3):
            result, calls = self.run_script("预算", [fixture.final("已完成"),
                fixture.tool("search_info_units", {"query": "入住"})])
        self.assertEqual(result["stop_reason"], "model_budget_exhausted")
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["summary_status"], "failed")

    def test_failed_proposal_can_retry_after_fetching_the_missing_source(self):
        proposal = suggestion("hypothesis")
        result, calls = self.run_script("核查损失并建立假设", [proposal_call(proposal, [], "missing"),
            fixture.tool("get_evidence", {"info_unit_ids": ["U029"]}, "read"), proposal_call(proposal, [], "corrected"),
            fixture.finish("核查损失并建立假设", ["U029"], refs=[("U029", 0)])],
            selected=["U022"], work_context={"reasoning": {"hypotheses": [], "gaps": []}}, issue_key=self.case.issue_set[1])
        error = json.loads(calls[1]["messages"][-1]["content"])["error"]
        self.assertEqual(error["validation_details"]["errors"][0]["code"], "reference_not_obtained")
        self.assertEqual(error["validation_details"]["errors"][0]["argument_loc"], ["info_unit_ids", 0])
        self.assertIn("get_evidence", error["recovery_hint"])
        self.assertEqual(len(result["proposals"]), 1)
        self.assertEqual(result["tool_attempt_count"], 5)
        self.assertEqual(result["summary_status"], "completed")

    def test_duplicate_read_reuses_cached_result_and_a_different_action_can_continue(self):
        original = tools.get_evidence
        with patch.object(tools, "get_evidence", wraps=original) as reader:
            result, _ = self.run_script("核对并扩展", [fixture.tool("get_evidence", {"info_unit_ids": ["U029"]}),
                fixture.tool("get_evidence", {"info_unit_ids": ["U029"]}, "duplicate"),
                fixture.tool("get_relations", {"info_unit_ids": ["U029"]}, "relations"),
                fixture.finish("核对并扩展", ["U029"], refs=[("U029", 0)])])
        self.assertEqual(reader.call_count, 1)
        self.assertEqual(result["stop_reason"], "finish_investigation")
        self.assertTrue(self.audits()[0]["tool_calls"][2]["result"]["cached"])

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
