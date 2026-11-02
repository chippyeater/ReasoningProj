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

    def gap(self, content, ids, call_id, **kwargs):
        return f.tool("propose_evidence_gap", {"content": content, "rationale": "同一时间条件可共同区分两种解释。",
            "basis_info_unit_ids": ["U029", "U036"], "evidence_refs": [],
            "hypothesis_targets": {"hypothesis_ids": [], "hypothesis_proposal_ids": ids}, **kwargs}, call_id)

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

    def test_competing_hypotheses_share_one_gap_and_synonymous_retries_do_not_append(self):
        contents = [
            "不存在实际租金损失：若新旧租约计租与实收连续覆盖，则没有租金断档。",
            "存在实际租金损失：若实际腾退后新租约延后计租且未收取租金，则存在断档损失。"]
        gap = "确认承租人（张某）实际腾退房屋的具体日期与时间（可区分是否存在空置）。"
        def respond(call, turn):
            state = self.state(call)
            proposals = state["proposals"]
            if turn == 1:
                return f.tool("search_info_units", {"query": "损失"}, "read")
            if turn in (2, 3):
                return self.hypothesis(contents[turn - 2], f"hyp-{turn}")
            ids = [p["proposal_id"] for p in proposals if p["kind"] == "hypothesis"]
            if turn == 4:
                return self.gap(gap, ids, "shared-gap")
            if turn == 5:
                # Same proposition with different rationale and material order.
                return self.hypothesis("不存在实际租金损失：新租客已接替入住；若计租连续，则无实收断档。", "synonym-hyp",
                    info_unit_ids=["U036", "U029"], rationale="补充理由也不构成新的假设。")
            if turn == 6:
                step = state["recovery"]["next_action"]["steps"][0]
                self.assertEqual(step["code"], "duplicate_proposal")
                self.assertEqual(step["existing_object"]["id"], ids[0])
                return self.gap("核实张某实际腾退（搬出）房屋的具体日期与时间：要求物业出入记录、钥匙交接凭证。",
                    list(reversed(ids)), "synonym-gap")
            step = state["recovery"]["next_action"]["steps"][0]
            self.assertEqual(step["code"], "duplicate_proposal")
            self.assertEqual(step["existing_object"]["id"], proposals[2]["proposal_id"])
            return f.finish("核查租金损失", questions=["尚待核实腾退和计租衔接。"],
                answer="两种解释仍取决于腾退与计租衔接的实际记录，已提交共同缺口供审阅。")
        context = self.context()
        before = deepcopy(context)
        result, _ = self.run_script("核查租金损失", respond, work_context=context, issue_key=self.case.issue_set[1])
        proposals = result["proposals"]
        self.assertEqual([p["kind"] for p in proposals], ["hypothesis", "hypothesis", "evidence_gap"])
        self.assertEqual(proposals[2]["hypothesis_proposal_ids"], [p["proposal_id"] for p in proposals[:2]])
        self.assertEqual(context, before)
        self.assertEqual(result["tool_budget_used"]["proposal"], 5)  # Failed duplicates still count.

    def test_formal_hypothesis_and_gap_are_reused_without_mutation(self):
        context = self.context()
        context["reasoning"]["hypotheses"] = [{"id": "formal-h", "content": "没有实际租金损失", "work_status": "deprecated"}]
        context["reasoning"]["gaps"] = [{"id": "formal-g", "content": "实际腾退日期是什么？",
            "hypothesis_ids": ["formal-h"], "basis_info_unit_ids": ["U029", "U036"]}]
        before = deepcopy(context)
        result, calls = self.run_script("复核已有对象", [
            f.tool("search_info_units", {"query": "损失"}, "read"),
            self.hypothesis("没有实际租金损失。", "same-hyp"),
            self.gap("实际腾退日期是什么", [], "same-gap", hypothesis_targets={"hypothesis_ids": ["formal-h"], "hypothesis_proposal_ids": []}),
            f.finish("复核已有对象")], work_context=context, issue_key=self.case.issue_set[1])
        self.assertEqual(result["proposals"], [])
        self.assertEqual(context, before)
        for call, uid in zip(calls[2:], ["formal-h", "formal-g"]):
            existing = self.state(call)["recovery"]["next_action"]["steps"][0]["existing_object"]
            self.assertEqual(existing["id"], uid)
            self.assertEqual(existing["source"], "reasoning")

    def test_distinct_gaps_and_numbered_conditions_are_not_merged(self):
        context = self.context()
        context["reasoning"]["hypotheses"] = [{"id": "formal-h", "content": "存在损失", "revision": 1}]
        targets = {"hypothesis_ids": ["formal-h"], "hypothesis_proposal_ids": []}
        result, _ = self.run_script("区分待核实条件", [
            f.tool("search_info_units", {"query": "损失"}, "read"),
            *[self.gap(content, [], f"gap-{i}", hypothesis_targets=targets) for i, content in enumerate([
                "实际腾退日期与时间是否早于指定日期（3月5日）？", "实际腾退日期与时间是否早于指定日期（3月8日）？", "新租约实际计租起点是什么？",
                "确认原租客实际腾退房屋的具体日期与时间", "确认新租客实际腾退房屋的具体日期与时间"])],
            f.finish("区分待核实条件")], work_context=context, issue_key=self.case.issue_set[1])
        self.assertEqual(len(result["proposals"]), 5)

    def test_gap_native_schema_and_argument_validation_require_nonempty_targets(self):
        schema = next(t["function"]["parameters"] for t in agent.tool_definitions() if t["function"]["name"] == "propose_evidence_gap")
        self.assertNotIn("anyOf", schema)  # Native strict roots remain objects.
        branches = schema["properties"]["hypothesis_targets"]["anyOf"]
        for branch in branches:
            self.assertEqual(branch["required"], ["hypothesis_ids", "hypothesis_proposal_ids"])
            self.assertFalse(branch["additionalProperties"])
            self.assertEqual(sum(p.get("minItems", 0) for p in branch["properties"].values()), 1)
        args = json.loads(self.gap("缺失条件", [], "bad").message["tool_calls"][0]["function"]["arguments"])
        for targets in ({"hypothesis_ids": [], "hypothesis_proposal_ids": []}, {}, [],
                        {"hypothesis_ids": "formal-h", "hypothesis_proposal_ids": []}):
            with self.subTest(targets=targets), self.assertRaises(AppError) as caught:
                agent._arguments("propose_evidence_gap", json.dumps({**args, "hypothesis_targets": targets}))
            error = caught.exception.details["errors"][0]
            self.assertEqual(error["loc"], ["hypothesis_targets"])
            self.assertEqual(error["code"], "hypothesis_target_required")

    def test_empty_or_guessed_gap_targets_reject_then_actual_pending_id_succeeds(self):
        def respond(call, turn):
            state = self.state(call)
            if turn == 1:
                return self.gap("需要核实的独立条件", [], "empty", basis_info_unit_ids=[])
            if turn == 2:
                self.assertEqual(state["recovery"]["next_action"]["steps"][0]["code"], "hypothesis_target_required")
                return self.hypothesis("独立待检验解释", "parent", info_unit_ids=[])
            if turn == 3:
                return self.gap("需要核实的独立条件", ["proposal-" + "f" * 32], "guessed", basis_info_unit_ids=[])
            if turn == 4:
                self.assertFalse(any(p["kind"] == "evidence_gap" for p in state["proposals"]))
                return self.gap("需要核实的独立条件", [state["proposals"][0]["proposal_id"]], "valid", basis_info_unit_ids=[])
            return f.finish("提出核实建议", status="completed", basis="general")
        result, _ = self.run_script("提出核实建议", respond, work_context=self.context(), issue_key=self.case.issue_set[1])
        self.assertEqual(len(result["proposals"]), 2)
        self.assertEqual(result["proposals"][1]["hypothesis_proposal_ids"], [result["proposals"][0]["proposal_id"]])

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
