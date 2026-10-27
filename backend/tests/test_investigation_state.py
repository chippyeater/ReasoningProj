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
    audits = f.TestInvestigationAgent.audits

    def run_script(self, goal, replies, selected=None, **kwargs):
        # State/step regressions start with an explicitly supplied departure
        # record; empty initial acquisition is covered at the raw loop boundary.
        return f.TestInvestigationAgent.run_execution_script(self, goal, replies,
            selected=["U022"] if selected is None else selected, **kwargs)

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
            if turn == 4:
                return f.tool("get_evidence", {"info_unit_ids": ["U036"], "question_ids": ["loss"]}, "opposing-source")
            return f.finish(goal, ["U026", "U029", "U036"], refs=[("U026", 0), ("U029", 0), ("U036", 0)], status="evidence_insufficient", updates=[
                f.question(goal, status="blocked", units=["U026", "U029"], reason="计租和收款证据缺失", block_kind="evidence_missing"),
                f.question(date["question"], "date", status="answered", units=["U026"], refs=[("U026", 0)], finding="张某陈述 3 月 8 日入住。"),
                f.question(loss["question"], "loss", status="blocked", units=["U029"], reason="缺少租约及收款记录", block_kind="evidence_missing")])
        case = self.case.model_copy(deep=True)
        case.info_units = [u for u in case.info_units if u.id in {"U026", "U029", "U036"}]
        case.relations = [r for r in case.relations if {r.source_id, r.target_id} <= {"U026", "U029", "U036"}]
        result, _ = self.run_script(goal, decide, case=case, selected=["U026"])
        self.assertEqual([q["status"] for q in result["question_state"]["questions"]], ["blocked", "answered", "blocked"])
        self.assertEqual(len(result["open_questions"]), 2)
        activities = self.audits()[0]["knowledge_state"]["activities"]
        self.assertEqual(activities[2]["question_ids"], ["date"])
        self.assertEqual(activities[3]["question_ids"], ["loss"])

    def test_unresolved_task_rejects_finish_then_recovers_atomically(self):
        goal = "核对"
        pending = f.question("还需核对另一方陈述", "Q2")
        result, calls = self.run_script(goal, [f.tool("update_questions", {"questions": [pending]}, "add"),
            f.finish(goal, ["U029"], refs=[("U029", 0)], status="completed", call_id="premature"),
            f.finish(goal, ["U029"], refs=[("U029", 0)], status="evidence_insufficient", updates=[
                f.question(goal, status="blocked", units=["U029"], reason="另一项请求仍缺独立佐证", block_kind="evidence_missing"),
                f.question(pending["question"], "Q2", status="blocked", reason="缺少独立核查材料", block_kind="evidence_missing")])], selected=["U029"])
        error = json.loads(calls[2]["messages"][-1]["content"])["error"]
        self.assertEqual(error["code"], "investigation_incomplete")
        self.assertEqual(error["validation_details"]["errors"][0]["loc"], ["questions", "Q2"])
        self.assertEqual(result["completion_status"], "evidence_insufficient")
        before_recovery = json.loads(calls[2]["messages"][1]["content"])["question_state"]["progress"]
        self.assertEqual([q["status"] for q in before_recovery], ["pending", "pending"])

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

    def test_steps_track_actual_work_and_proposal_does_not_discharge_pending_work(self):
        def decide(call, turn):
            state = json.loads(call["messages"][1]["content"])
            if turn == 1:
                return f.tool("update_investigation_steps", {"steps": [f.step(), f.step("S2", objective="核对独立收款证明")]}, "plan")
            if turn == 2:
                return f.tool("get_evidence", {"info_unit_ids": ["U029"], "step_id": "S1"}, "read")
            if turn == 3:
                self.assertEqual(state["investigation_steps"]["progress"][0]["status"], "in_progress")
                self.assertEqual(state["investigation_steps"]["progress"][0]["activity_indices"], [2])
                return proposal_call(suggestion("hypothesis"), [], "hyp")
            if turn == 4:
                return f.tool("update_investigation_steps", {"steps": [f.step(status="completed", note="已核对陈述，尚非独立证实")]}, "handled")
            if turn == 5:
                return f.finish("核对", ["U029"], refs=[("U029", 0)], status="evidence_insufficient", call_id="premature")
            if turn == 6:
                error = json.loads(call["messages"][-1]["content"])["error"]
                self.assertEqual(error["validation_details"]["errors"][0]["code"], "step_unresolved")
                self.assertEqual(error["validation_details"]["errors"][0]["repair_tool"], "finish_investigation")
                self.assertEqual(state["investigation_steps"]["progress"][1]["status"], "pending")
                return f.tool("update_investigation_steps", {"steps": [f.step("S2", status="deferred", objective="核对独立收款证明", note="本轮已获得的材料仅含双方陈述，独立账目需另行取得")]}, "defer")
            return f.finish("核对", ["U029"], refs=[("U029", 0)], status="evidence_insufficient", updates=[])
        result, calls = self.run_script("核对", decide, work_context={"reasoning": {"hypotheses": [], "gaps": []}}, issue_key=self.case.issue_set[1])
        self.assertEqual(len(calls), 7)
        steps = result["question_state"]["investigation_steps"]
        self.assertEqual([s["status"] for s in steps], ["completed", "deferred"])
        self.assertEqual(steps[0]["activity_indices"], [2])
        activities = self.audits()[0]["knowledge_state"]["activities"]
        self.assertEqual(activities[2]["step_id"], "S1")
        self.assertEqual(activities[3]["tool"], "propose_hypothesis")
        self.assertIsNone(activities[3]["step_id"])
        self.assertEqual(result["tool_budget_used"]["question"], 4)

    def test_steps_can_complete_from_initial_material_without_reads_or_proposals(self):
        result, calls = self.run_script("核对", [
            f.tool("update_investigation_steps", {"steps": [f.step(status="completed", note="已核对当前上下文中提供的陈述及来源", basis=["U029"])]}, "handled"),
            f.finish("核对", ["U029"], refs=[("U029", 0)])], selected=["U029"])
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["proposals"], [])
        self.assertEqual(result["tool_budget_used"]["read"], 0)
        self.assertEqual(result["question_state"]["investigation_steps"][0]["activity_indices"], [])

    def test_arbitrary_note_cannot_fake_investigation_without_material_or_execution(self):
        result, calls = self.run_script("核对", [
            f.tool("update_investigation_steps", {"steps": [f.step(status="completed", note="我已搜索并核验所有证据")]}, "fake"),
            f.finish("核对")])
        self.assertEqual(json.loads(calls[1]["messages"][-1]["content"])["error"]["code"], "step_completion_without_basis")
        self.assertEqual(result["question_state"]["investigation_steps"][0]["status"], "deferred")

    def test_all_deferred_cannot_answer_question_and_remains_visible_in_audit(self):
        result, calls = self.run_script("核对", [
            f.tool("update_investigation_steps", {"steps": [f.step(status="deferred", note="当前仅有单方陈述，暂不扩大本轮调查")]}, "defer"),
            f.finish("核对", ["U029"], refs=[("U029", 0)], call_id="false-complete"),
            f.finish("核对", ["U029"], refs=[("U029", 0)], status="evidence_insufficient")], selected=["U029"])
        errors = json.loads(calls[2]["messages"][-1]["content"])["error"]["validation_details"]["errors"]
        self.assertIn("all_steps_deferred", {e["code"] for e in errors})
        self.assertEqual(result["question_state"]["questions"][0]["status"], "blocked")
        self.assertTrue(any("全部延期" in note for note in result["limitations"]))
        self.assertEqual(self.audits()[0]["question_state"]["investigation_steps"][0]["activity_indices"], [])

    def test_empty_search_is_handled_work_without_asserting_fact_absence(self):
        result, _ = self.run_script("核对", [
            f.tool("update_investigation_steps", {"steps": [f.step()]}, "plan"),
            f.tool("search_info_units", {"query": "保险赔付", "step_id": "S1"}, "empty"),
            f.tool("update_investigation_steps", {"steps": [f.step(status="completed", note="本查询未命中，不等于全案不存在相关材料")]}, "handled"),
            f.finish("核对")])
        self.assertEqual(result["completion_status"], "evidence_insufficient")
        self.assertEqual(result["question_state"]["investigation_steps"][0]["activity_indices"], [2])
        self.assertEqual(self.audits()[0]["knowledge_state"]["activities"][2]["status"], "empty")

    def test_invalid_step_or_question_scope_never_executes_read(self):
        for args, expected in (({"step_id": "foreign"}, "invalid_step_reference"),
                               ({"step_id": ["S1"]}, "invalid_step_reference"),
                               ({"step_id": "S1", "question_ids": ["facts", "Q2"]}, "step_question_mismatch"),
                               ({"step_id": "S1", "question_ids": ["unknown"]}, "invalid_question_reference")):
            with self.subTest(args=args):
                result, calls = self.run_script("核对", [
                    f.tool("update_questions", {"questions": [f.question("子问题", "Q2")]}, "question"),
                    f.tool("update_investigation_steps", {"steps": [f.step()]}, "plan"),
                    f.tool("get_evidence", {"info_unit_ids": ["U029"], **args}, "invalid"),
                    f.tool("update_investigation_steps", {"steps": [f.step(status="deferred", note="暂不继续当前子问题，需独立材料")]}, "defer"),
                    f.finish("核对", updates=[f.question("核对", status="blocked", reason="需独立材料", block_kind="evidence_missing"),
                        f.question("子问题", "Q2", status="blocked", reason="需独立材料", block_kind="evidence_missing")])])
                self.assertEqual(json.loads(calls[3]["messages"][-1]["content"])["error"]["code"], expected)
                self.assertEqual(result["question_state"]["investigation_steps"][0]["activity_indices"], [])
                self.assertFalse(self.audits()[-1]["tool_calls"][3]["executed"])

    def test_cached_reads_keep_real_activity_indices_and_do_not_fake_progress(self):
        result, _ = self.run_script("核对", [
            f.tool("update_investigation_steps", {"steps": [f.step()]}, "plan"),
            *[f.tool("search_info_units", {"query": "损失", "step_id": "S1"}, f"read-{i}") for i in range(4)]])
        self.assertEqual(result["stop_reason"], "no_progress_limit")
        activities = self.audits()[0]["knowledge_state"]["activities"]
        self.assertTrue(activities[2]["executed"])
        self.assertTrue(all(not a["executed"] and a["cached"] for a in activities[3:]))
        self.assertEqual(result["question_state"]["investigation_steps"][0]["activity_indices"], [2, 3, 4, 5])
        self.assertEqual(result["question_state"]["investigation_steps"][0]["status"], "in_progress")
        self.assertIsNone(result["answer"])

    def test_step_owner_is_used_when_question_ids_omitted_for_relations(self):
        result, _ = self.run_script("核对", [
            f.tool("update_questions", {"questions": [f.question("核对关系", "Q2")]}, "question"),
            f.tool("update_investigation_steps", {"steps": [f.step("S2", qid="Q2")]}, "plan"),
            f.tool("get_relations", {"info_unit_ids": ["U029"], "step_id": "S2"}, "relations"),
            f.tool("update_investigation_steps", {"steps": [f.step("S2", qid="Q2", status="completed", note="已查看持久化关系，仍缺独立证明")]}, "handled"),
            f.finish("核对", updates=[f.question("核对", status="blocked", reason="需独立证明", block_kind="evidence_missing"),
                f.question("核对关系", "Q2", status="blocked", reason="需独立证明", block_kind="evidence_missing")])])
        activity = self.audits()[0]["knowledge_state"]["activities"][3]
        self.assertEqual(activity["question_ids"], ["Q2"])
        self.assertEqual(activity["step_id"], "S2")
        self.assertEqual(self.audits()[0]["knowledge_state"]["activities"][2]["question_ids"], ["Q2"])
        self.assertEqual(result["question_state"]["investigation_steps"][1]["activity_indices"], [3])

    def test_tool_error_is_recorded_and_can_be_deferred_without_becoming_evidence(self):
        result, _ = self.run_script("核对", [
            f.tool("update_investigation_steps", {"steps": [f.step()]}, "plan"),
            f.tool("get_evidence", {"info_unit_ids": ["foreign"], "step_id": "S1"}, "failed-read"),
            f.tool("update_investigation_steps", {"steps": [f.step(status="deferred", note="来源请求失败，本轮尚未取得所需材料")]}, "defer"),
            f.finish("核对", updates=[f.question("核对", status="blocked", reason="来源请求失败，尚需核查有效材料", block_kind="tool_failure")])])
        activity = self.audits()[0]["knowledge_state"]["activities"][2]
        self.assertEqual(activity["status"], "invalid_request")
        self.assertTrue(activity["executed"])
        self.assertEqual(activity["obtained"], [])
        self.assertEqual(result["question_state"]["investigation_steps"][0]["activity_indices"], [2])

    def test_step_and_question_updates_share_existing_budget(self):
        replies = []
        for i in range(4):
            replies.append(f.tool("update_questions", {"questions": [f.question(f"子问题{i}", "Q2")]}, f"q-{i}"))
            replies.append(f.tool("update_investigation_steps", {"steps": [f.step(objective=f"核查目标{i}",
                status="deferred" if i == 3 else "pending", note="待外部独立材料" if i == 3 else None)]}, f"step-{i}"))
        replies.extend([f.tool("update_investigation_steps", {"steps": [f.step("new")]}, "exhausted"),
            f.finish("核对", updates=[f.question("核对", status="blocked", reason="待外部独立材料", block_kind="evidence_missing"),
                f.question("子问题3", "Q2", status="blocked", reason="待外部独立材料", block_kind="evidence_missing")])])
        result, calls = self.run_script("核对", replies)
        self.assertEqual(result["tool_budget_used"]["question"], 8)
        names = {d["function"]["name"] for d in calls[8]["definitions"]}
        self.assertNotIn("update_questions", names)
        self.assertNotIn("update_investigation_steps", names)
        self.assertEqual(json.loads(calls[9]["messages"][-1]["content"])["error"]["code"], "tool_budget_exhausted")

    def test_finish_can_close_pending_step_after_all_update_budget_is_used(self):
        replies = [f.tool("update_investigation_steps", {"steps": [f.step()]}, "plan")]
        replies.extend(f.tool("update_questions", {"questions": [f.question("核对", status="in_progress", finding=f"继续核对第{i}项条件")]}, f"q-{i}")
                       for i in range(7))
        replies.extend([f.tool("get_evidence", {"info_unit_ids": ["U029"], "step_id": "S1"}, "read"),
            f.finish("核对", ["U029"], refs=[("U029", 0)], step_updates=[f.step(status="completed", note="已核对陈述来源，不等于独立证实")])])
        result, calls = self.run_script("核对", replies)
        self.assertEqual(result["stop_reason"], "finish_investigation")
        self.assertEqual(result["tool_budget_used"], {"read": 1, "proposal": 0, "question": 8, "finish": 1})
        self.assertEqual(result["tool_attempt_count"], 11)
        self.assertEqual(result["question_state"]["investigation_steps"][0]["activity_indices"], [9])
        self.assertEqual(result["question_state"]["investigation_steps"][0]["status"], "completed")
        names = {d["function"]["name"] for d in calls[-1]["definitions"]}
        self.assertNotIn("update_questions", names)
        self.assertNotIn("update_investigation_steps", names)
        self.assertEqual(result["proposals"], [])

    def test_reopening_clears_basis_and_requires_new_read_or_explicit_material(self):
        result, calls = self.run_script("核对", [
            f.tool("update_investigation_steps", {"steps": [f.step()]}, "plan"),
            f.tool("get_evidence", {"info_unit_ids": ["U029"], "step_id": "S1"}, "read"),
            f.tool("update_investigation_steps", {"steps": [f.step(status="completed", note="已核对", basis=["U029"])]}, "handled"),
            f.tool("update_investigation_steps", {"steps": [f.step(status="in_progress", note="重新核查条件")]}, "reopen"),
            f.finish("核对", ["U029"], refs=[("U029", 0)], call_id="old-read-not-enough", step_updates=[f.step(status="completed", note="已处理")]),
            f.tool("get_evidence", {"info_unit_ids": ["U029"], "step_id": "S1"}, "access-again"),
            f.finish("核对", ["U029"], refs=[("U029", 0)], step_updates=[f.step(status="completed", note="重新检查已提供的来源")])])
        recovery = json.loads(calls[5]["messages"][-1]["content"])["error"]
        self.assertIn("step_completion_without_basis", {e["code"] for e in recovery["validation_details"]["errors"]})
        self.assertFalse(recovery["validation_details"]["step_updates_applied"])
        self.assertFalse(recovery["validation_details"]["question_updates_applied"])
        state = json.loads(calls[5]["messages"][1]["content"])
        reopened = state["investigation_steps"]["details"]["items"][0]
        self.assertEqual(reopened["basis_info_unit_ids"], [])
        self.assertEqual(reopened["activity_indices"], [2])
        self.assertEqual(reopened["activity_start_index"], 4)
        self.assertEqual(state["question_state"]["progress"][0]["status"], "pending")
        step = result["question_state"]["investigation_steps"][0]
        self.assertEqual(step["activity_indices"], [2, 6])
        self.assertEqual(step["activity_start_index"], 4)
        activity = self.audits()[0]["knowledge_state"]["activities"][6]
        self.assertTrue(activity["cached"])
        self.assertFalse(activity["executed"])
        old_completion = self.audits()[0]["knowledge_state"]["activities"][3]["arguments"]["steps"][0]
        self.assertEqual(old_completion["basis_info_unit_ids"], ["U029"])

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
        questions = QuestionState()
        questions.items["facts"] = f.agent.Question.model_validate(f.question("核对材料"))
        questions.activities["facts"] = []
        questions.update_steps([f.step(f"S{i}", objective="核对材料陈述" * 100) for i in range(20)], self.case, memory)
        before_questions = questions.dump()
        view = model_context(initial, memory, questions, {"total": 10}, None)
        self.assertLessEqual(len(json.dumps(view, ensure_ascii=False)), MAX_CONTEXT_CHARS)
        self.assertTrue(view["knowledge_state"]["info_units"]["context_truncated"])
        self.assertTrue(view["knowledge_state"]["source_refs"]["items"])
        self.assertEqual(memory.dump(), before)
        self.assertEqual(questions.dump(), before_questions)
        self.assertEqual(len(view["investigation_steps"]["progress"]), 20)
        self.assertTrue(all(s["objective"] and s["objective_truncated"] for s in view["investigation_steps"]["progress"]))
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
