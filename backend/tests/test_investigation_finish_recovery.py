"""Finish validation remains actionable after the investigation phase closes."""
import json
import unittest

import test_investigation_agent as f


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
                    "findings": [], "open_questions": "应为问题列表", "completion_status": "evidence_insufficient"}, "bad-finish")
                def respond(call, turn):
                    if turn <= len(reads):
                        return reads[turn - 1]
                    self.assertEqual([d["function"]["name"] for d in call["definitions"]], ["finish_investigation"])
                    if turn == len(reads) + 1:
                        return bad
                    state = json.loads(call["messages"][1]["content"])
                    recovery = state["recovery"]
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
        case, reads = self.closing_reads("no_progress")
        _, calls = self.run_script("核对", [*reads,
            f.finish("核对", ["U029"], refs=[("U029", 99)], call_id="bad-finish"),
            f.finish("核对", questions=["来源索引无效，结论尚缺有效依据。"])], case=case, selected=["U029"])
        state = json.loads(calls[-1]["messages"][1]["content"])
        recovery = state["recovery"]
        error = next(e for e in recovery["validation_details"]["errors"] if e["code"] == "reference_out_of_range")
        self.assertEqual(error["info_unit_id"], "U029")
        self.assertEqual(error["info_unit_title"], next(u.title for u in case.info_units if u.id == "U029"))
        self.assertEqual(error["source_ref_index"], 99)
        self.assertIn("get_evidence", error["original_fix"])
        self.assertIn("open_questions", error["fix"])
        for step in recovery["next_action"]["steps"]:
            self.assertEqual(step["tool"], "finish_investigation")
            self.assertEqual(step["unavailable_tool"], "get_evidence")
            self.assertNotIn("arguments", step)
        self.assertEqual([d["function"]["name"] for d in calls[-1]["definitions"]], ["finish_investigation"])

    def test_closed_phase_still_limits_failed_finish_submissions_to_three(self):
        case, reads = self.closing_reads("no_progress")
        result, calls = self.run_script("核对", [*reads,
            *[f.tool("finish_investigation", {"bad_field": i}, f"bad-finish-{i}") for i in range(3)]], case=case)
        self.assertEqual(result["tool_budget_used"]["finish"], 3)
        self.assertEqual(result["summary_status"], "failed")
        self.assertTrue(all([d["function"]["name"] for d in c["definitions"]] == ["finish_investigation"] for c in calls[-3:]))
