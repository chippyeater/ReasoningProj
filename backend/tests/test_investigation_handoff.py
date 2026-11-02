"""Offline regressions for material handoff, pending identity and finish gating."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

import test_investigation_agent as f
from app import investigation_agent as agent, investigation_actions as actions
from app.errors import AppError


class TestInvestigationHandoff(unittest.TestCase):
    setUp = f.TestInvestigationAgent.setUp
    tearDown = f.TestInvestigationAgent.tearDown
    run_script = f.TestInvestigationAgent.run_script
    audits = f.TestInvestigationAgent.audits

    def context(self):
        return {"current_issue": self.case.issue_set[1], "reasoning": {"hypotheses": [], "gaps": [], "links": []}}

    def state(self, call):
        return json.loads(call["messages"][1]["content"])

    def hypothesis(self, content, call_id, **kwargs):
        return f.tool("propose_hypothesis", {"content": content, "rationale": "待核实成立条件，不能以单方陈述证明。",
            "info_unit_ids": ["U029", "U036"], "evidence_refs": [], **kwargs}, call_id)

    def test_three_search_hits_remain_available_but_unread_sources_cannot_be_cited(self):
        hits = ["U006", "U008", "U010"]
        query = " ".join(next(u.title for u in self.case.info_units if u.id == uid) for uid in hits)
        def respond(call, turn):
            state = self.state(call)
            if turn == 1:
                return f.tool("search_info_units", {"query": query, "limit": 3}, "search")
            units = {u["id"]: u for u in state["materials"]["info_units"]["items"]}
            self.assertEqual(len(units), 3)
            self.assertTrue(all(u["content"] for u in units.values()))
            if turn == 2:
                self.assertTrue(all(u["obtained_source_ref_indices"] == [] for u in units.values()))
                self.assertFalse(state["materials"]["source_refs"]["items"])
                uid = next(iter(units))
                return f.finish("核对押金条款", [uid], refs=[(uid, 0)], call_id="unread-source")
            if turn == 3:
                recovery = state["recovery"]
                error = recovery["validation_details"]["errors"][0]
                self.assertEqual(error["code"], "reference_not_obtained")
                self.assertIn(error["info_unit_id"], units)
                return f.tool("get_evidence", {"info_unit_ids": [error["info_unit_id"]]}, "source")
            pointer = state["materials"]["source_refs"]["items"][0]
            uid, index = pointer["info_unit_id"], pointer["source_ref_index"]
            self.assertIn(index, units[uid]["obtained_source_ref_indices"])
            return f.finish("核对押金条款", [uid], refs=[(uid, index)], call_id="corrected")
        result, _ = self.run_script("核对押金条款", respond)
        self.assertEqual(result["summary_status"], "completed")
        self.assertEqual(result["tool_budget_used"], {"read": 2, "proposal": 0, "finish": 2})

    def test_finish_required_is_sticky_and_blocks_out_of_schema_tools_before_dispatch(self):
        bad_finish = f.tool("finish_investigation", {"answer": "可帮助核对来源。"}, "invalid-finish")
        with patch.dict(actions.PROPOSAL_TOOLS, {"propose_hypothesis": unittest.mock.Mock(side_effect=AssertionError("must not execute"))}):
            result, calls = self.run_script("介绍能力", [f.final("调查已经完成，可以交付。"),
                self.hypothesis("不应再创建的新解释", "blocked", info_unit_ids=[]), bad_finish,
                f.finish("介绍能力", status="completed", basis="general", answer="可检索信息、核对来源并提出待审阅建议。")],
                work_context=self.context(), issue_key=self.case.issue_set[1])
        self.assertEqual(result["proposals"], [])
        self.assertEqual(result["summary_status"], "completed")
        self.assertEqual(result["tool_budget_used"]["finish"], 2)
        self.assertFalse(self.audits()[0]["tool_calls"][0]["executed"])
        for call in calls[1:]:
            self.assertEqual([t["function"]["name"] for t in call["definitions"]], ["finish_investigation"])
            self.assertEqual(call["tool_choice"], "required")
            self.assertEqual(self.state(call)["recovery"]["constraints"]["allowed_tools"], ["finish_investigation"])
        errors = self.state(calls[-1])["recovery"]["validation_details"]["errors"]
        self.assertTrue(any(e["argument_loc"] == ["answer_basis"] for e in errors))

    def test_finish_required_does_not_grant_source_permission_or_exhaust_shared_budget(self):
        result, calls = self.run_script("核对损失", [
            f.tool("search_info_units", {"query": "损失"}, "read"), f.final("资料取得，可以结束。"),
            f.finish("核对损失", ["U029"], refs=[("U029", 0)], call_id="unread-source"),
            f.finish("核对损失", questions=["没有取得该陈述的原始来源，无法核实。"], call_id="limited")])
        step = self.state(calls[-1])["recovery"]["next_action"]["steps"][0]
        self.assertEqual(step["code"], "reference_not_obtained")
        self.assertEqual(step["tool"], "finish_investigation")
        self.assertEqual(step["unavailable_tool"], "get_evidence")
        self.assertFalse(result["evidence_refs"])
        self.assertEqual(result["completion_status"], "evidence_insufficient")
        self.assertEqual(result["tool_budget_used"], {"read": 1, "proposal": 0, "finish": 2})
