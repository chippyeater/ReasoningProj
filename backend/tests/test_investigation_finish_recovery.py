"""Finish validation remains actionable after the investigation phase closes."""
import json
import unittest

import test_investigation_agent as f
from app.errors import AppError


class TestInvestigationFinishRecovery(unittest.TestCase):
    setUp = f.TestInvestigationAgent.setUp
    tearDown = f.TestInvestigationAgent.tearDown
    run_script = f.TestInvestigationAgent.run_script
    audits = f.TestInvestigationAgent.audits

    def closing_reads(self, reason):
        case = self.case.model_copy(deep=True)
        if reason == "budget":
            case.info_units = [case.info_units[0].model_copy(update={"id": f"budget-{i}",
                "title": f"预算材料{i}", "content": "独立信息", "source_refs": []}) for i in range(8)]
            case.relations = []
            return case, [f.tool("search_info_units", {"query": f"预算材料{i}"}, f"read-{i}") for i in range(8)]
        return case, [f.tool("search_info_units", {"query": f"不存在凭证{i}"}, f"read-{i}") for i in range(3)]

    def test_closed_phase_preserves_finish_parameter_errors_until_success(self):
        for reason in ("budget", "no_progress"):
            with self.subTest(reason=reason):
                case, reads = self.closing_reads(reason)
                bad = f.tool("finish_investigation", {"answer": "仍缺核验材料。", "answer_basis": "case_material",
                    "open_questions": "应为问题列表", "completion_status": "evidence_insufficient"}, "bad-finish")
                def respond(call, turn):
                    if turn <= len(reads):
                        return reads[turn - 1]
                    self.assertEqual([d["function"]["name"] for d in call["definitions"]], ["finish_investigation"])
                    if turn == len(reads) + 1:
                        return bad
                    state = json.loads(call["messages"][1]["content"])
                    recovery = state["previous_tool_result"]["recovery"]
                    error = recovery["validation_details"]["errors"][0]
                    self.assertEqual(error["argument_loc"], ["open_questions"])
                    self.assertEqual(error["code"], "list_type")
                    self.assertIn("list", error["message"])
                    self.assertIn("list", error["fix"])
                    step = recovery["next_action"]["steps"][0]
                    self.assertEqual(step["loc"], ["open_questions"])
                    self.assertEqual(step["instruction"], error["fix"])
                    self.assertEqual(recovery["constraints"]["allowed_tools"], ["finish_investigation"])
                    return f.finish("核对", status="budget_exhausted" if reason == "budget" else "evidence_insufficient")
                result, _ = self.run_script("核对", respond, case=case)
                self.assertEqual(result["summary_status"], "completed")
                self.assertEqual(result["tool_budget_used"]["finish"], 2)

    def test_unavailable_read_recommendation_preserves_reference_details(self):
        from app import investigation_agent as agent, investigation_tools as tools
        case, _ = self.closing_reads("no_progress")
        # Reference repair remains necessary for Proposal errors after tools close.
        response = tools._failure(AppError("invalid_investigation_reference", "Unobtained material"))
        response["error"]["validation_details"] = {"errors": [{
                "loc": ["proposals", 0, "info_unit_ids", 0], "code": "reference_not_obtained",
                "info_unit_id": "U029"}]}
        agent._tool_recovery(response, "propose_hypothesis", case=case)
        recovery = response["error"]
        agent._finish_only_recovery(recovery)
        error = next(e for e in recovery["validation_details"]["errors"] if e["code"] == "reference_not_obtained")
        self.assertEqual(error["info_unit_id"], "U029")
        self.assertEqual(error["info_unit_title"], next(u.title for u in case.info_units if u.id == "U029"))
        self.assertIn("search_info_units", error["original_fix"])
        self.assertIn("open_questions", error["fix"])
        for step in recovery["next_action"]["steps"]:
            self.assertEqual(step["tool"], "finish_investigation")
            self.assertEqual(step["unavailable_tool"], "search_info_units")
            self.assertNotIn("arguments", step)

    def test_closed_phase_still_limits_failed_finish_submissions_to_three(self):
        case, reads = self.closing_reads("no_progress")
        result, calls = self.run_script("核对", [*reads,
            *[f.tool("finish_investigation", {"bad_field": i}, f"bad-finish-{i}") for i in range(3)]], case=case)
        self.assertEqual(result["tool_budget_used"]["finish"], 3)
        self.assertEqual(result["summary_status"], "failed")
        self.assertTrue(all([d["function"]["name"] for d in c["definitions"]] == ["finish_investigation"] for c in calls[-3:]))
