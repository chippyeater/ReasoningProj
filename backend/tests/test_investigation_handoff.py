"""Offline regressions for material handoff, pending identity and finish gating."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

import test_investigation_agent as f
from app import investigation_agent as agent, investigation_actions as actions
from app.errors import AppError
from app import investigation_tools as tools


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
            "info_unit_ids": ["U029", "U036"], **kwargs}, call_id)

    def gap(self, content, ids, call_id, **kwargs):
        return f.tool("propose_evidence_gap", {"content": content, "rationale": "同一时间条件可共同区分两种解释。",
            "basis_info_unit_ids": ["U029", "U036"],
            "hypothesis_targets": {"hypothesis_ids": [], "hypothesis_proposal_ids": ids}, **kwargs}, call_id)

    def comparison_context(self):
        context = self.context()
        context["reasoning"]["hypotheses"] = [
            {"id": "formal-a", "content": "原有解释甲", "revision": 1},
            {"id": "formal-b", "content": "原有解释乙", "revision": 1}]
        context["reasoning"]["gaps"] = [{"id": "formal-gap", "content": "原有核查问题",
            "hypothesis_ids": ["formal-a"], "basis_info_unit_ids": [], "revision": 1}]
        from app.issue_workspaces import get_issue_workspaces, persist_issue_workspaces
        from app.reasoning_models import Hypothesis, EvidenceGap
        self._workspace_enabled = True
        current = get_issue_workspaces(self.case)
        state = current.workspaces[self.case.issue_set[1]].reasoning
        state.hypotheses = [Hypothesis(**h, updated_at="test") for h in context["reasoning"]["hypotheses"]]
        state.gaps = [EvidenceGap(**g, updated_at="test") for g in context["reasoning"]["gaps"]]
        persist_issue_workspaces(self.case, current)
        rows = [row for uid in ("U029", "U036") for row in tools.get_evidence(self.case, uid)["items"]]
        refs = [{"info_unit_id": row["info_unit_id"], "source_ref_index": row["source_ref_index"]}
                for row in rows]
        seen = {"units": {ref["info_unit_id"] for ref in refs}, "relations": set(),
                "evidence": {(ref["info_unit_id"], ref["source_ref_index"]) for ref in refs}}
        return context, seen, refs

    def submit_pending(self, kind, content, proposals, context, seen, refs, **changes):
        values = {"content": content, "rationale": "根据已取得的材料提出待核查命题。"}
        if kind == "hypothesis":
            values["info_unit_ids"] = ["U029", "U036"]
        else:
            values.update(basis_info_unit_ids=["U029", "U036"], hypothesis_ids=["formal-a"])
        values.update(changes)
        return actions._propose(self.case, seen, context, proposals, kind, **values)

    def test_exact_and_format_only_duplicates_return_original_pending_id_without_mutation(self):
        context, seen, refs = self.comparison_context()
        pairs = [
            ("实际租金损失仍待核实", "实际租金损失仍待核实"),
            ("H1：租金收款连续", "H2: 租金收款连续"),
            ("假设1：租金收款连续", "租金收款连续"),
            ("房东，发生损失。", "房东 发生损失!"),
            ("房东：可能存在损失（有待核实）。", "房东: 可能存在损失 (有待核实)!"),
            ("确认实际腾退日期", "核实实际腾退日期"),
            ("核对实际腾退日期", "查明实际腾退日期"),
            ("confirm Tenant ACME has paid", "verify tenant acme has paid"),
            ("tenant pays rent", " tenant  pays   rent "),
            ("ＡＣＭＥ已支付１０００元", "acme已支付1000元")]
        before_files = self.fixture.disk_snapshot()
        before_context, before_seen = deepcopy(context), deepcopy(seen)
        for kind in ("hypothesis", "evidence_gap"):
            for first, duplicate in pairs:
                with self.subTest(kind=kind, first=first, duplicate=duplicate):
                    proposals = []
                    original = self.submit_pending(kind, first, proposals, context, seen, refs)
                    before = [p.model_dump(mode="json") for p in proposals]
                    with self.assertRaises(AppError) as caught:
                        self.submit_pending(kind, duplicate, proposals, context, seen, refs)
                    self.assertEqual(caught.exception.code, "duplicate_proposal")
                    existing = caught.exception.details["errors"][0]["existing_object"]
                    self.assertEqual(existing["id"], original["proposal_id"])
                    self.assertEqual(existing["source"], "proposals")
                    self.assertEqual([p.model_dump(mode="json") for p in proposals], before)
                    self.assertEqual(context, before_context)
                    self.assertEqual(seen, before_seen)
                    self.assertEqual(self.fixture.disk_snapshot(), before_files)

    def test_substantive_subjects_conditions_and_numeric_symbols_create_distinct_proposals(self):
        context, seen, refs = self.comparison_context()
        pairs = [
            ("租金损失：提前退租造成空置", "租金损失：新租约租金较低产生差额"),
            ("房东实际租金损失：提前退租造成空置", "房东实际租金损失：新租约租金较低产生差额"),
            ("确认实际承租人（张某）的腾退日期", "确认实际承租人（李某）的腾退日期"),
            ("确认承租人（甲公司）的腾退日期", "确认承租人（乙公司）的腾退日期"),
            ("确认业主（张某）的腾退日期", "确认租客（张某）的腾退日期"),
            ("承租人腾退日期（3月5日）需核实", "承租人腾退日期（3月8日）需核实"),
            ("少收租金1000元", "少收租金2000元"),
            ("空置3天", "空置5天"),
            ("存在实际租金损失", "不存在实际租金损失"),
            ("租金损失（已收租）", "租金损失（未收租）"),
            ("新租金低于原租金", "新租金高于原租金"),
            ("因空置导致少收租金", "因少收租金导致空置"),
            ("租金差额1.5元", "租金差额15元"),
            ("租金差额.5元", "租金差额5元"),
            ("租金差额-100元", "租金差额100元"),
            ("租金差额+100元", "租金差额-100元"),
            ("新租金<原租金", "新租金>原租金"),
            ("新租金!=原租金", "新租金=原租金"),
            ("新租金 ! = 原租金", "新租金=原租金"),
            ("空置天数≤3", "空置天数≥3"),
            ("租金降幅5%", "租金降幅5"),
            ("应付租金为原租金的1/2", "应付租金为原租金的12"),
            ("租金比例1:2", "租金比例12"),
            ("租金比例1 : 2", "租金比例12"),
            ("金额1,5元", "金额15元"),
            ("金额1 , 5元", "金额15元"),
            ("金额1 . 5元", "金额15元"),
            ("损失金额1*(2+3)", "损失金额1*2+3"),
            ("待核数量5!", "待核数量5"),
            ("待核数量(1;2)", "待核数量(12)"),
            ("tenant is not able to pay", "tenant is notable to pay")]
        before_context, before_seen = deepcopy(context), deepcopy(seen)
        before_files = self.fixture.disk_snapshot()
        for kind in ("hypothesis", "evidence_gap"):
            for first, second in pairs:
                with self.subTest(kind=kind, first=first, second=second):
                    proposals = []
                    a = self.submit_pending(kind, first, proposals, context, seen, refs)
                    b = self.submit_pending(kind, second, proposals, context, seen, refs)
                    self.assertEqual([p.content for p in proposals], [first, second])
                    self.assertNotEqual(a["proposal_id"], b["proposal_id"])
        self.assertEqual(context, before_context)
        self.assertEqual(seen, before_seen)
        self.assertEqual(self.fixture.disk_snapshot(), before_files)

    def test_rationale_reference_order_and_associations_do_not_rename_a_proposition(self):
        context, seen, refs = self.comparison_context()
        for kind in ("hypothesis", "evidence_gap"):
            with self.subTest(kind=kind):
                proposals = []
                original = self.submit_pending(kind, "需要核查的完整命题：条件仍待确认", proposals, context, seen, refs)
                before = [p.model_dump(mode="json") for p in proposals]
                changes = {"rationale": "换一种理由仍然是同一命题。"}
                changes["info_unit_ids" if kind == "hypothesis" else "basis_info_unit_ids"] = ["U036", "U029"]
                if kind == "evidence_gap":
                    changes["hypothesis_ids"] = ["formal-b"]
                with self.assertRaises(AppError) as caught:
                    self.submit_pending(kind, proposals[0].content, proposals, context, seen, list(reversed(refs)), **changes)
                self.assertEqual(caught.exception.code, "duplicate_proposal")
                self.assertEqual(caught.exception.details["errors"][0]["existing_object"]["id"], original["proposal_id"])
                self.assertEqual([p.model_dump(mode="json") for p in proposals], before)

    def test_duplicate_guard_does_not_extend_to_links_status_or_gap_updates(self):
        context, seen, refs = self.comparison_context()
        ref = [next(ref for ref in refs if ref["info_unit_id"] == "U029")]
        cases = {
            "evidence_link": {"content": None, "hypothesis_ids": ["formal-a"], "info_unit_ids": ["U029"], "meaning": "related"},
            "hypothesis_status": {"content": None, "hypothesis_ids": ["formal-a"], "info_unit_ids": ["U029"], "judgment": "unverified"},
            "evidence_gap_update": {"gap_id": "formal-gap", "addressing_info_unit_ids": ["U029"], "status": "open"}}
        before_context, before_seen = deepcopy(context), deepcopy(seen)
        before_files = self.fixture.disk_snapshot()
        for kind, values in cases.items():
            with self.subTest(kind=kind):
                proposals = []
                for _ in range(2):
                    actions._propose(self.case, seen, context, proposals, kind, rationale="单独审阅该建议。",  **values)
                self.assertEqual(len(proposals), 0 if kind == "hypothesis_status" else 1)
        self.assertEqual(context, before_context)
        self.assertEqual(seen, before_seen)
        self.assertEqual(self.fixture.disk_snapshot(), before_files)

    def test_search_hits_can_ground_proposals_without_mandatory_original_reads(self):
        hits = ["U006", "U008", "U010"]
        query = " ".join(next(u.title for u in self.case.info_units if u.id == uid) for uid in hits)
        def respond(call, turn):
            state = self.state(call)
            if turn == 1:
                return f.tool("search_info_units", {"query": query, "limit": 3}, "search")
            units = {u["id"]: u for u in state["materials"]["info_units"]["items"]}
            self.assertEqual(len(units), 3)
            self.assertFalse(state["materials"]["source_refs"]["items"])
            if turn == 2:
                return f.tool("propose_hypothesis", {"content": "押金扣除可能缺少依据", "rationale": "条款尚需核实",
                    "info_unit_ids": [next(iter(units))]}, "summary-proposal")
            return f.finish('核对押金条款', status='completed')
        result, _ = self.run_script("核对押金条款", respond, issue_key=self.case.issue_set[1])
        self.assertEqual(result["summary_status"], "completed")
        self.assertEqual(result["tool_budget_used"], {"read": 1, "proposal": 1, "finish": 1})

    def test_competing_hypotheses_share_one_gap_and_format_only_retries_do_not_append(self):
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
                return self.hypothesis(contents[0].replace("：", ": ").replace("。", "！"), "same-hyp",
                    info_unit_ids=["U036", "U029"], rationale="补充理由也不构成新的假设。")
            if turn == 6:
                step = state["previous_tool_result"]["recovery"]["next_action"]["steps"][0]
                self.assertEqual(step["code"], "duplicate_proposal")
                self.assertEqual(step["existing_object"]["id"], ids[0])
                return self.gap(gap.replace("确认", "核实", 1).replace("（", "(").replace("）", ")"),
                    list(reversed(ids)), "same-gap")
            step = state["previous_tool_result"]["recovery"]["next_action"]["steps"][0]
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
            existing = self.state(call)["previous_tool_result"]["recovery"]["next_action"]["steps"][0]["existing_object"]
            self.assertEqual(existing["id"], uid)
            self.assertEqual(existing["source"], "reasoning")

    def test_distinct_gaps_and_numbered_conditions_are_not_merged(self):
        context, _, _ = self.comparison_context()
        context["reasoning"]["hypotheses"] = [context["reasoning"]["hypotheses"][0]]
        context["reasoning"]["gaps"] = []
        targets = {"hypothesis_ids": ["formal-a"], "hypothesis_proposal_ids": []}
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
                self.assertEqual(state["previous_tool_result"]["recovery"]["next_action"]["steps"][0]["code"], "hypothesis_target_required")
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
            previous = self.state(call)["previous_tool_result"]
            if previous is None:  # No actual tool execution has occurred yet.
                self.assertIn("validated Finish", call["messages"][0]["content"])
            else:
                self.assertEqual(previous["recovery"]["constraints"]["allowed_tools"], ["finish_investigation"])
        errors = self.state(calls[-1])["previous_tool_result"]["recovery"]["validation_details"]["errors"]
        self.assertTrue(any(e["argument_loc"] == ["answer_basis"] for e in errors))

    def test_finish_required_keeps_obtained_material_without_granting_sources(self):
        result, calls = self.run_script("核对损失", [
            f.tool("search_info_units", {"query": "损失"}, "read"), f.final("资料取得，可以结束。"),
            f.finish("核对损失", questions=["没有取得该陈述的原始来源，无法核实。"], call_id="limited")])
        step = self.state(calls[-1])["previous_tool_result"]["recovery"]["next_action"]["steps"][0]
        self.assertEqual(step["code"], "finish_required")
        self.assertEqual(step["tool"], "finish_investigation")
        self.assertEqual(self.state(calls[-1])["previous_tool_result"]["recovery"]["constraints"]["allowed_tools"], ["finish_investigation"])
        self.assertFalse(result["evidence_refs"])
        self.assertEqual(result["completion_status"], "evidence_insufficient")
        self.assertEqual(result["tool_budget_used"], {"read": 1, "proposal": 0, "finish": 1})
