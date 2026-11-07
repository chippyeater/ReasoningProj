"""Offline recovery and shared-budget contracts; no paid model calls."""
import json
import unittest
from unittest.mock import patch

import test_investigation_agent as f
from app import investigation_actions as actions, investigation_tools as tools


class TestInvestigationRecoveryBudget(unittest.TestCase):
    setUp = f.TestInvestigationAgent.setUp
    tearDown = f.TestInvestigationAgent.tearDown
    run_script = f.TestInvestigationAgent.run_script
    audits = f.TestInvestigationAgent.audits

    def context(self):
        return {"current_issue": self.case.issue_set[1], "reasoning": {"hypotheses": [], "gaps": [], "links": []}}

    def recovery(self, call):
        return json.loads(call["messages"][1]["content"])["recovery"]

    def hypothesis(self, index, ids=()):
        return f.tool("propose_hypothesis", {"content": f"待核查解释{index}", "rationale": "只是待检验解释。",
            "info_unit_ids": list(ids)}, f"hyp-{index}")

    def mixed_calls(self):
        return [f.tool("get_evidence", {"info_unit_id": uid}, f"read-{i}")
                for i, uid in enumerate(("U029", "U036", "U026", "U004"))] + [self.hypothesis(i) for i in range(4)]

    def test_four_reads_four_proposals_share_budget_and_finish_remains(self):
        with patch.object(actions, "finish_investigation", wraps=actions.finish_investigation) as finish_tool:
            result, calls = self.run_script("核对", [*self.mixed_calls(), f.finish("核对", status="budget_exhausted")],
                work_context=self.context(), issue_key=self.case.issue_set[1])
        self.assertTrue(finish_tool.call_args.kwargs["read_budget_exhausted"])
        self.assertTrue(finish_tool.call_args.kwargs["budget_exhausted"])
        self.assertEqual(result["tool_budget_used"], {"read": 4, "proposal": 4, "finish": 1})
        self.assertEqual(result["completion_status"], "budget_exhausted")
        self.assertEqual([d["function"]["name"] for d in calls[-1]["definitions"]], ["finish_investigation"])
        self.assertEqual(result["tool_attempt_count"], 9)
        self.assertEqual(self.recovery(calls[-1])["next_action"]["steps"][0]["tool"], "finish_investigation")

    def test_ninth_non_finish_is_rejected_before_dispatch(self):
        result, calls = self.run_script("核对", [*self.mixed_calls(), self.hypothesis(9),
            f.finish("核对", status="budget_exhausted")], work_context=self.context(), issue_key=self.case.issue_set[1])
        audit = self.audits()[0]
        self.assertFalse(audit["tool_calls"][8]["executed"])
        self.assertEqual(audit["tool_calls"][8]["result"]["error"]["code"], "tool_budget_exhausted")
        self.assertEqual(len(result["proposals"]), 4)
        self.assertEqual(result["tool_budget_used"]["proposal"], 4)

    def test_failed_calls_consume_shared_budget(self):
        replies = []
        for i in range(4):
            replies += [f.tool("search_info_units", {"query": "", "unexpected": i}, f"bad-{i}"), self.hypothesis(i)]
        result, calls = self.run_script("核对", [*replies, f.finish("核对", status="budget_exhausted")],
            work_context=self.context(), issue_key=self.case.issue_set[1])
        self.assertEqual(result["tool_budget_used"], {"read": 4, "proposal": 4, "finish": 1})
        self.assertEqual([d["function"]["name"] for d in calls[-1]["definitions"]], ["finish_investigation"])

    def test_finish_neither_increments_nor_resets_no_progress_and_can_recover(self):
        bad = f.tool("finish_investigation", {"answer": "结论尚无依据。", "answer_basis": "case_material",
            "open_questions": [], "completion_status": "evidence_insufficient"}, "bad-finish")
        result, calls = self.run_script("核对", [
            f.tool("search_info_units", {"query": "不存在凭证甲"}, "empty-1"), bad,
            f.tool("search_info_units", {"query": "不存在凭证乙"}, "empty-2"),
            f.tool("search_info_units", {"query": "不存在凭证丙"}, "empty-3"),
            f.finish("核对", status="evidence_insufficient")])
        self.assertEqual([c["no_progress"] for c in self.audits()[0]["tool_calls"]], [1, 1, 2, 3, 3])
        step = self.recovery(calls[2])["next_action"]["steps"][0]
        self.assertEqual(step["code"], "missing_evidence_limitation")
        self.assertEqual(step["loc"], ["open_questions"])
        self.assertIn("open_questions", step["instruction"])
        self.assertEqual([d["function"]["name"] for d in calls[-1]["definitions"]], ["finish_investigation"])
        self.assertEqual(result["summary_status"], "completed")

    def test_consecutive_finish_failures_are_limited_to_three(self):
        replies = [f.tool("finish_investigation", {"bad_field": i}, f"bad-{i}") for i in range(3)]
        result, calls = self.run_script("核对", replies)
        self.assertEqual(result["tool_budget_used"], {"read": 0, "proposal": 0, "finish": 3})
        self.assertEqual([c["no_progress"] for c in self.audits()[0]["tool_calls"]], [0, 0, 0])
        self.assertEqual(result["completion_status"], "evidence_insufficient")
        self.assertEqual(result["summary_status"], "failed")
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(step["loc"] for step in self.recovery(calls[1])["next_action"]["steps"]))

    def test_finish_failure_after_no_progress_can_be_corrected(self):
        bad = f.tool("finish_investigation", {"answer": "缺少核验材料。", "answer_basis": "case_material",
            "open_questions": [], "completion_status": "evidence_insufficient"}, "bad-finish")
        with patch.object(actions, "finish_investigation", wraps=actions.finish_investigation) as finish_tool:
            result, calls = self.run_script("核对", [
                *[f.tool("search_info_units", {"query": f"不存在凭证{i}"}, f"empty-{i}") for i in range(3)],
                bad, f.finish("核对")])
        self.assertEqual([c["no_progress"] for c in self.audits()[0]["tool_calls"]], [1, 2, 3, 3, 3])
        self.assertTrue(finish_tool.call_args.kwargs["read_budget_exhausted"])
        self.assertFalse(finish_tool.call_args.kwargs["budget_exhausted"])
        self.assertEqual(self.recovery(calls[-1])["next_action"]["retry_tool"], "finish_investigation")
        self.assertEqual(result["summary_status"], "completed")
        self.assertEqual(result["tool_budget_used"]["finish"], 2)

    def test_six_valid_proposals_do_not_exhaust_shared_budget(self):
        result, calls = self.run_script("核对", [*[self.hypothesis(i) for i in range(6)],
            f.finish("核对", status="budget_exhausted", call_id="premature"),
            f.tool("search_info_units", {"query": "月租"}, "read"), f.finish("核对")],
            work_context=self.context(), issue_key=self.case.issue_set[1], selected=["U029"])
        names = {d["function"]["name"] for d in calls[6]["definitions"]}
        self.assertIn("search_info_units", names)
        self.assertFalse(any(name.startswith("propose_") for name in names))
        self.assertEqual(self.recovery(calls[7])["code"], "budget_available")
        self.assertEqual(len(result["proposals"]), 6)
        self.assertEqual(result["completion_status"], "evidence_insufficient")

    def test_multiple_missing_materials_have_ids_titles_and_actual_read_recovery(self):
        def respond(call, turn):
            state = json.loads(call["messages"][1]["content"])
            if turn == 1:
                return self.hypothesis(0, ["U029", "U036"])
            if turn == 2:
                recovery = state["recovery"]
                errors = recovery["validation_details"]["errors"]
                self.assertEqual({e["info_unit_id"] for e in errors}, {"U029", "U036"})
                for error in errors:
                    self.assertEqual(error["info_unit_title"], next(u.title for u in self.case.info_units if u.id == error["info_unit_id"]))
                    self.assertEqual(error["code"], "reference_not_obtained")
                    self.assertEqual(error["loc"][:1], ["relevant_info_unit_ids"])
                self.assertFalse(state["materials"]["info_units"]["items"])
                self.assertFalse(state["materials"]["source_refs"]["items"])
                action = recovery["next_action"]["steps"][0]
                return f.tool(action["tool"], action["arguments"], "recover-search-a")
            if turn == 3:
                return f.tool("search_info_units", {"query": next(u.title for u in self.case.info_units if u.id == "U036")}, "recover-search-b")
            if turn == 4:
                return self.hypothesis(1, ["U029", "U036"])
            return f.finish("核对")
        result, _ = self.run_script("核对", respond, work_context=self.context(), issue_key=self.case.issue_set[1])
        self.assertEqual(len(result["proposals"]), 1)
        self.assertEqual(result["proposals"][0]["info_unit_ids"], ["U029", "U036"])

    def test_unobtained_info_unit_recovery_carries_title_and_search_action(self):
        from test_reasoning_proposals import suggestion, proposal_call
        _, calls = self.run_script("核对", [proposal_call(suggestion("hypothesis"), [], "bad"),
            f.tool("get_evidence", {"info_unit_id": "U029"}, "recover"),
            f.finish('核对', status='completed')], issue_key=self.case.issue_set[1])
        errors = self.recovery(calls[1])["validation_details"]["errors"]
        error = next(e for e in errors if e["code"] == "reference_not_obtained")
        self.assertEqual(error["info_unit_id"], "U029")
        self.assertTrue(error["info_unit_title"])
        self.assertEqual(error["next_action"]["tool"], "search_info_units")

    def test_failure_recovery_does_not_encourage_blind_retry(self):
        with patch.object(tools, "get_evidence", side_effect=OSError("unavailable")):
            _, calls = self.run_script("核对", [f.tool("get_evidence", {"info_unit_id": "U029"}, "failed"),
                f.finish("核对")])
        self.assertEqual(self.recovery(calls[1])["next_action"]["steps"][0]["code"], "investigation_tool_failed")
        self.assertFalse(json.loads(calls[1]["messages"][1]["content"])["materials"]["source_refs"]["items"])

    def test_duplicate_recovery_reuses_existing_results(self):
        duplicate = self.hypothesis(0)
        duplicate.message["tool_calls"][0]["id"] = "duplicate"
        result, calls = self.run_script("核对", [self.hypothesis(0), duplicate,
            f.finish("核对", status="completed", basis="general")], work_context=self.context(), issue_key=self.case.issue_set[1])
        self.assertEqual(self.recovery(calls[2])["next_action"]["steps"][0]["code"], "duplicate_query")
        self.assertEqual(len(result["proposals"]), 1)
