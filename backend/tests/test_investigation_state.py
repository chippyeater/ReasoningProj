"""Task progress, state compaction and validated finishing, with Case A mocks."""
import json
import unittest
from copy import deepcopy

import test_investigation_agent as f
from test_reasoning_proposals import proposal_call, suggestion
from app.investigation_state import KnowledgeState, QuestionState, MAX_CONTEXT_CHARS, model_context


class TestInvestigationState(unittest.TestCase):
    setUp = f.TestInvestigationAgent.setUp
    tearDown = f.TestInvestigationAgent.tearDown
    run_script = f.TestInvestigationAgent.run_script
    audits = f.TestInvestigationAgent.audits

    def test_multi_question_investigation_records_activity_without_automatic_answer(self):
        goal = "核对入住及损失"
        root = f.question(goal, status="in_progress")
        date = f.question("入住日期是什么", "date", status="in_progress")
        loss = f.question("能否确认损失", "loss")
        def decide(call, turn):
            if turn == 1:
                return f.tool("update_questions", {"questions": [root, date, loss]}, "plan")
            if turn == 2:
                return f.tool("get_evidence", {"info_unit_ids": ["U026"], "question_ids": ["date"]}, "date-source")
            if turn == 3:
                state = json.loads(call["messages"][1]["content"])
                q = next(q for q in state["question_state"]["details"]["items"] if q["id"] == "date")
                self.assertEqual(q["status"], "in_progress")
                self.assertEqual(q["evidence_refs"], [{"info_unit_id": "U026", "source_ref_index": 0}])
                return f.tool("get_evidence", {"info_unit_ids": ["U029"], "question_ids": ["loss"]}, "loss-source")
            return f.finish(goal, ["U026", "U029"], refs=[("U026", 0), ("U029", 0)], updates=[
                f.question(goal, status="blocked", units=["U026", "U029"], reason="计租和收款证据缺失", block_kind="evidence_missing"),
                f.question(date["question"], "date", status="answered", units=["U026"], refs=[("U026", 0)], finding="张某陈述 3 月 8 日入住。"),
                f.question(loss["question"], "loss", status="blocked", units=["U029"], reason="缺少租约及收款记录", block_kind="evidence_missing")])
        result, _ = self.run_script(goal, decide)
        self.assertEqual([q["status"] for q in result["question_state"]["questions"]], ["blocked", "answered", "blocked"])
        self.assertEqual(len(result["open_questions"]), 2)
        activities = self.audits()[0]["knowledge_state"]["activities"]
        self.assertEqual(activities[1]["question_ids"], ["date"])
        self.assertEqual(activities[2]["question_ids"], ["loss"])

    def test_unresolved_task_rejects_finish_then_recovers_atomically(self):
        goal = "核对"
        pending = f.question("还需核对另一方陈述", "Q2")
        result, calls = self.run_script(goal, [f.tool("update_questions", {"questions": [pending]}, "add"),
            f.finish(goal, ["U029"], refs=[("U029", 0)], status="completed", call_id="premature"),
            f.finish(goal, ["U029"], refs=[("U029", 0)], updates=[
                f.question(goal, status="answered", units=["U029"], finding="已读张某陈述"),
                f.question(pending["question"], "Q2", status="blocked", reason="缺少独立核查材料", block_kind="evidence_missing")])], selected=["U029"])
        error = json.loads(calls[2]["messages"][-1]["content"])["error"]
        self.assertEqual(error["code"], "investigation_incomplete")
        self.assertEqual(error["validation_details"]["errors"][0]["loc"], ["questions", "Q2"])
        self.assertEqual(result["completion_status"], "evidence_insufficient")
        before_recovery = json.loads(calls[2]["messages"][1]["content"])["question_state"]["progress"]
        self.assertEqual([q["status"] for q in before_recovery], ["pending", "pending"])

    def test_two_planned_explanations_cannot_finish_after_only_one_proposal(self):
        goal = "比较解释"
        plans = [{"id": str(i), "kind": "hypothesis", "description": content,
                  "proposal_id": None, "omission_reason": None} for i, content in enumerate(("可能少收租金", "计租可能连续"))]
        root = f.question(goal, status="in_progress", plans=plans)
        def decide(call, turn):
            if turn == 1:
                return f.tool("update_questions", {"questions": [root]}, "plan")
            if turn == 2:
                return proposal_call({**suggestion("hypothesis"), "content": plans[0]["description"]}, [], "hyp")
            state = json.loads(call["messages"][1]["content"])
            pid = state["knowledge_state"]["proposals"]["items"][0]["proposal_id"]
            updated = deepcopy(plans)
            updated[0]["proposal_id"] = pid
            if turn == 4:
                error = json.loads(call["messages"][-1]["content"])["error"]
                self.assertEqual(error["validation_details"]["errors"][0]["code"], "unresolved_plan")
                updated[1]["omission_reason"] = "与当前已采纳解释重复；不新增同义假设。"
            return f.finish(goal, ["U029"], refs=[("U029", 0)], call_id=f"finish-{turn}", updates=[
                f.question(goal, status="blocked", units=["U029"], reason="缺收款证据", block_kind="evidence_missing", plans=updated)])
        result, calls = self.run_script(goal, decide, selected=["U029"],
            work_context={"reasoning": {"hypotheses": [], "gaps": []}}, issue_key=self.case.issue_set[1])
        self.assertEqual(len(calls), 4)
        self.assertEqual(len(result["proposals"]), 1)
        self.assertTrue(result["question_state"]["questions"][0]["planned_actions"][1]["omission_reason"])

    def test_registered_plan_cannot_be_silently_deleted_or_relabelled(self):
        plan = {"id": "h", "kind": "hypothesis", "description": "可能少收租金", "proposal_id": None, "omission_reason": None}
        result, _ = self.run_script("核对", [f.tool("update_questions", {"questions": [f.question("核对", plans=[plan])]}, "plan"),
            f.tool("update_questions", {"questions": [f.question("核对")]}, "delete"),
            f.tool("update_questions", {"questions": [f.question("核对", plans=[{**plan, "description": "替换候选"}])]}, "replace"),
            f.finish("核对", updates=[f.question("核对", status="blocked", reason="缺少收款证据", block_kind="evidence_missing",
                plans=[{**plan, "omission_reason": "缺少检验路径，暂不提出"}])])])
        self.assertEqual([c["result"]["error"]["code"] for c in self.audits()[0]["tool_calls"][1:3]], ["unresolved_plan"]*2)
        self.assertEqual(result["summary_status"], "completed")

    def test_missing_evidence_does_not_require_gap_but_legal_gap_is_supported(self):
        def decide(call, turn):
            if turn == 1:
                return proposal_call({**suggestion("hypothesis"), "info_unit_ids": [], "evidence_refs": []}, [], "h")
            if turn == 2:
                pid = json.loads(call["messages"][-1]["content"])["proposal_id"]
                return proposal_call({**suggestion("evidence_gap"), "info_unit_ids": [], "evidence_refs": [], "hypothesis_proposal_ids": [pid]}, [], "gap")
            return f.finish("查损失", questions=["缺计租日和收款记录"])
        result, _ = self.run_script("查损失", decide, work_context={"reasoning": {"hypotheses": [], "gaps": []}}, issue_key=self.case.issue_set[1])
        self.assertEqual([p["kind"] for p in result["proposals"]], ["hypothesis", "evidence_gap"])
        self.assertEqual(result["completion_status"], "evidence_insufficient")
        self.assertEqual(result["findings"], [])

    def test_question_reference_block_reason_and_answer_basis_are_checked_atomically(self):
        for bad in (f.question("核对", status="blocked"), f.question("核对", status="answered", finding="已核实"),
                    f.question("核对", status="answered", units=["foreign"], finding="已核实")):
            with self.subTest(bad=bad):
                result, _ = self.run_script("核对", [f.tool("update_questions", {"questions": [f.question("新问题", "Q2"), bad]}, "bad"),
                    f.finish("核对")])
                self.assertEqual(len(result["question_state"]["questions"]), 1)

    def test_context_budget_preserves_content_and_marks_omissions_without_mutating_audit_state(self):
        initial = {"goal": "核对", "selected_info_units": [], "evidence_context": {"items": [], "errors": [], "has_more": False}}
        memory = KnowledgeState(initial)
        for i in range(100):
            memory.info_units[str(i)] = {"id": str(i), "type": "claim", "content": "单方陈述"*2000, "speaker": "某人"}
            memory.source_refs[str(i)] = {"info_unit_id": str(i), "source_ref_index": 0, "source_ref": {"quote": "原文"*3000}}
        before = memory.dump()
        view = model_context(initial, memory, QuestionState("核对"), {"total": 10}, None)
        self.assertLessEqual(len(json.dumps(view, ensure_ascii=False)), MAX_CONTEXT_CHARS)
        self.assertTrue(view["knowledge_state"]["info_units"]["context_truncated"])
        self.assertTrue(view["knowledge_state"]["source_refs"]["items"])
        self.assertEqual(memory.dump(), before)
        row = view["knowledge_state"]["info_units"]["items"][0]
        self.assertIn("单方陈述", row["content"]["excerpt"])
        self.assertEqual(row["speaker"], "某人")

    def test_finish_validates_budget_claims_and_empty_or_missing_question_updates(self):
        cases = []
        for status in ("completed", "budget_exhausted"):
            cases.append(f.finish("核对", status=status, call_id="invalid"))
        malformed = f.finish("核对", call_id="invalid")
        args = json.loads(malformed.message["tool_calls"][0]["function"]["arguments"])
        args["question_updates"] = False
        cases.append(f.tool("finish_investigation", args, "invalid"))
        for bad in cases:
            with self.subTest(call=bad):
                result, calls = self.run_script("核对", [bad, f.finish("核对", call_id="fixed")])
                self.assertEqual(json.loads(calls[1]["messages"][-1]["content"])["status"], "invalid_request")
                self.assertEqual(result["completion_status"], "evidence_insufficient")

    def test_answered_question_cannot_hide_its_source_from_final_findings(self):
        result, calls = self.run_script("核对", [f.finish("核对", ["U029"], refs=[("U029", 0)], status="completed", call_id="bad",
            updates=[f.question("核对", status="answered", units=["U029", "U036"], refs=[("U029", 0), ("U036", 0)], finding="两方陈述有分歧")]),
            f.finish("核对", ["U029", "U036"], refs=[("U029", 0), ("U036", 0)], status="completed")], selected=["U029", "U036"])
        error = json.loads(calls[1]["messages"][-1]["content"])["error"]
        self.assertEqual(error["validation_details"]["errors"][0]["code"], "question_basis_not_delivered")
        self.assertEqual(len(result["evidence_refs"]), 2)
