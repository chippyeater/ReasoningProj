"""Final delivery error-chain regressions; native tools, no model/network calls."""
from copy import deepcopy
import json
import unittest

import test_investigation_agent as f
import test_investigation_sufficiency as sufficiency
from test_reasoning_proposals import proposal_call, suggestion
from app import investigation_actions as actions, investigation_agent as agent
from app.errors import AppError
from app import investigation_tools as tools


class TestInvestigationDelivery(unittest.TestCase):
    setUp = f.TestInvestigationAgent.setUp
    tearDown = f.TestInvestigationAgent.tearDown
    run_script = f.TestInvestigationAgent.run_execution_script
    audits = f.TestInvestigationAgent.audits
    claims_case = sufficiency.TestInvestigationSufficiency.claims_case
    state = sufficiency.TestInvestigationSufficiency.state
    finish_args = sufficiency.TestInvestigationSufficiency.finish_args
    context = {"reasoning": {"hypotheses": [], "gaps": []}}

    def proposal(self, case, knowledge, seen, proposals, *, kind="hypothesis", qids=("facts",), units=("U029",), **extra):
        values = {"content": "待核实是否存在实际空置损失", "rationale": "陈述需要独立收款材料核实",
                  "info_unit_ids": list(units), "evidence_refs": [{"info_unit_id": uid, "source_ref_index": 0} for uid in units], **extra}
        if kind == "evidence_gap":
            values.setdefault("hypothesis_ids", [])
            values.setdefault("hypothesis_proposal_ids", [proposals[0].proposal_id])
        response = getattr(actions, "propose_" + kind)(case, seen, self.context, proposals, **values)
        knowledge.merge("propose_" + kind, values, response, list(qids))
        return response["proposal_id"]

    def blocked(self, units=("U029",)):
        return f.question("核对", status="blocked", units=list(units), reason="缺独立收款凭证", block_kind="evidence_missing")

    def test_incremental_steps_keep_identity_and_history_without_proposal_bindings(self):
        case, knowledge, questions, seen = self.state()
        questions.update_steps([f.step(), f.step("S2")], case, knowledge)
        questions.update_steps([f.step(status="completed", objective="检查上下文已有陈述", note="已检查，但尚无独立佐证", basis=["U029"])], case, knowledge)
        self.assertEqual(list(questions.steps), ["S1", "S2"])
        with self.assertRaises(AppError) as caught:
            actions.finish_investigation(case, seen, self.context, "核对", [], questions,
                knowledge=knowledge, **self.finish_args(updates=[self.blocked()]))
        self.assertIn("step_unresolved", {e["code"] for e in caught.exception.details["errors"]})
        questions.update_steps([f.step("S2", status="deferred", note="当前材料不足以核验独立收款")], case, knowledge)
        result, draft, _ = actions.finish_investigation(case, seen, self.context, "核对", [], questions,
            knowledge=knowledge, **self.finish_args(updates=[]))
        self.assertEqual(result.proposals, [])
        self.assertEqual(draft.steps["S1"].status, "completed")
        self.assertEqual(draft.steps["S2"].status, "deferred")

    def test_step_scope_terminal_history_and_forged_activity_rejected_atomically(self):
        from pydantic import ValidationError
        case, knowledge, questions, seen = self.state()
        questions.update([f.question("另一问题", "Q2")], case, seen, self.context)
        questions.update_steps([f.step(status="completed", note="已处理上下文材料", basis=["U029"])], case, knowledge)
        before = questions.dump()
        for row in (f.step(qid="Q2", status="completed", note="换问题"),
                    f.step(status="pending"), f.step(status="completed", objective="改为别的目标", note="换目标"),
                    {**f.step(), "activity_indices": [0]},
                    {**f.step(), "activity_start_index": 0},
                    f.step("S2", qid="unknown"), f.step("S2", status="deferred")):
            with self.subTest(row=row), self.assertRaises((AppError, ValidationError)):
                questions.update_steps([f.step("new"), row], case, knowledge)
            self.assertEqual(questions.dump(), before)
        questions.update_steps([f.step(status="in_progress", note="重新核查")], case, knowledge)
        self.assertEqual(questions.steps["S1"].status, "in_progress")
        self.assertEqual(questions.steps["S1"].activity_indices, [])

    def test_question_updates_cannot_delete_registered_steps(self):
        case, knowledge, questions, seen = self.state()
        questions.update_steps([f.step()], case, knowledge)
        questions.update([self.blocked()], case, seen, self.context)
        self.assertIn("S1", questions.steps)
        with self.assertRaises(AppError) as caught:
            actions.finish_investigation(case, seen, self.context, "核对", [], questions,
                knowledge=knowledge, **self.finish_args(updates=[]))
        self.assertEqual(caught.exception.details["errors"][0]["code"], "step_unresolved")

    def test_question_has_no_delivery_plan_and_rejects_removed_field(self):
        case, knowledge, questions, seen = self.state()
        self.assertNotIn("planned_actions", questions.items["facts"].model_dump())
        with self.assertRaises(AppError) as caught:
            questions.update([{**f.question("核对"), "planned_actions": []}], case, seen, self.context)
        self.assertEqual(caught.exception.details["errors"][0]["code"], "extra_forbidden")
        self.assertEqual(questions.items["facts"].status, "pending")

    def test_shared_hypothesis_dependency_keeps_real_question_scope(self):
        case, knowledge, questions, seen = self.state()
        proposals = []
        questions.update([f.question("检验共同假设", "Q2")], case, seen, self.context)
        hid = self.proposal(case, knowledge, seen, proposals, qids=("Q2",))
        gid = self.proposal(case, knowledge, seen, proposals, kind="evidence_gap")
        questions.update([self.blocked()], case, seen, self.context)
        self.assertEqual(proposals[1].hypothesis_proposal_ids, [hid])
        self.assertEqual([(a["proposal_id"], a["question_ids"]) for a in knowledge.activities], [(hid, ["Q2"]), (gid, ["facts"])])
        self.assertEqual(questions.steps, {})

    def test_proposal_unknown_question_cannot_create_or_reassign_suggestion(self):
        source = suggestion("hypothesis")
        source.update(info_unit_ids=[], evidence_refs=[])
        reply = proposal_call(source, [], "foreign-question")
        call = reply.message["tool_calls"][0]
        args = json.loads(call["function"]["arguments"])
        args["question_ids"] = ["foreign"]
        result, calls = self.run_script("核对", [f.tool("propose_hypothesis", args, "invalid"), f.finish("核对")],
            selected=["U022"], work_context=deepcopy(self.context), issue_key=self.case.issue_set[1])
        self.assertEqual(json.loads(calls[1]["messages"][-1]["content"])["error"]["code"], "invalid_question_reference")
        self.assertEqual(result["proposals"], [])

    def test_first_finish_reports_step_question_relation_source_and_summary_errors(self):
        case, knowledge, questions, seen = self.state(ids=("U029",))
        proposals = []
        self.proposal(case, knowledge, seen, proposals)
        self.proposal(case, knowledge, seen, proposals, kind="evidence_gap")
        questions.update_steps([f.step()], case, knowledge)
        updates = [{**self.blocked(), "question": ""}]
        args = self.finish_args(updates=updates)
        args.update(answer="字" * 241, findings=[{"statement": "两方陈述待核实", "info_unit_ids": ["U029"],
            "relation_ids": ["R010"], "evidence_refs": [{"info_unit_id": "U029", "source_ref_index": 99}]}])
        before = questions.dump()
        with self.assertRaises(AppError) as caught:
            actions.finish_investigation(case, seen, self.context, "核对", proposals, questions, knowledge=knowledge, **args)
        details = caught.exception.details
        codes = {e["code"] for e in details["errors"]}
        self.assertTrue({"step_unresolved", "string_too_short", "reference_not_obtained", "reference_out_of_range", "summary_too_long"} <= codes)
        self.assertEqual(details["errors"][0]["argument_loc"], ["question_updates", 0, "question"])
        self.assertTrue(any(e.get("argument_loc") == ["findings", 0, "relation_ids", 0] for e in details["errors"]))
        self.assertTrue(any(e.get("argument_loc") == ["findings", 0, "evidence_refs", 0] for e in details["errors"]))
        self.assertTrue(all(e["loc"] and e["message"] and e["fix"] for e in details["errors"]))
        self.assertFalse(details["question_updates_applied"])
        self.assertEqual(questions.dump(), before)
        self.assertEqual(knowledge.findings, [])

    def test_malformed_question_and_finding_do_not_hide_other_independent_errors(self):
        case, knowledge, questions, seen = self.state(ids=("U029",))
        args = self.finish_args(updates=[self.blocked(), {"id": [], "status": "answered"}])
        args.update(answer="字" * 241, findings=[7, {"statement": "无来源", "info_unit_ids": ["foreign"], "relation_ids": [], "evidence_refs": []}])
        with self.assertRaises(AppError) as caught:
            actions.finish_investigation(case, seen, self.context, "核对", [], questions, knowledge=knowledge, **args)
        codes = {e["code"] for e in caught.exception.details["errors"]}
        self.assertTrue({"missing", "model_type", "reference_not_found", "summary_too_long"} <= codes)
        self.assertTrue(any(e.get("argument_loc") == ["question_updates", 1, "question"] for e in caught.exception.details["errors"]))
        self.assertEqual(questions.items["facts"].status, "pending")

    def test_validated_question_batch_survives_failed_delivery_and_empty_updates_reuse_it(self):
        def decide(call, turn):
            if turn == 1:
                return f.tool("update_investigation_steps", {"steps": [f.step(status="completed", note="已核对当前材料", basis=["U029"])]}, "handle")
            if turn == 2:
                return f.finish("核对", ["U029"], refs=[("U029", 0)], answer="字" * 241, call_id="bad-answer")
            state = json.loads(call["messages"][1]["content"])
            error = json.loads(call["messages"][-1]["content"])["error"]
            self.assertTrue(error["validation_details"]["question_updates_applied"])
            self.assertEqual(state["question_state"]["progress"][0]["status"], "answered")
            self.assertEqual(state["knowledge_state"]["findings"]["items"], [])
            return f.finish("核对", ["U029"], refs=[("U029", 0)], updates=[], answer="已核对陈述，仍需独立核实。")
        result, calls = self.run_script("核对", decide, selected=["U029"])
        self.assertEqual(result["stop_reason"], "finish_investigation")
        self.assertEqual(len(calls), 3)

    def test_native_error_chain_recovers_without_losing_steps_questions_or_proposals(self):
        goal = "核对"
        def decide(call, turn):
            if turn == 1:
                return f.tool("update_investigation_steps", {"steps": [f.step()]}, "plan")
            if turn == 2:
                return f.tool("get_evidence", {"info_unit_ids": ["U029"], "step_id": "S1"}, "read")
            state = json.loads(call["messages"][1]["content"])
            ids = [p["proposal_id"] for p in state["knowledge_state"]["proposals"]["items"]]
            if turn == 3:
                return proposal_call(suggestion("hypothesis"), [], "hypothesis")
            if turn == 4:
                return proposal_call({**suggestion("evidence_gap"), "hypothesis_proposal_ids": ids}, [], "gap")
            if turn == 5:
                return f.tool("update_investigation_steps", {"steps": [f.step(status="completed", note="核对了原始陈述，尚需对方材料")]}, "handled")
            if turn == 7:
                error = json.loads(call["messages"][-1]["content"])["error"]
                self.assertIn("reference_not_obtained", {e["code"] for e in error["validation_details"]["errors"]})
                self.assertTrue(error["validation_details"]["question_updates_applied"])
                self.assertEqual(state["investigation_steps"]["progress"][0]["activity_indices"], [2])
                self.assertEqual(state["investigation_steps"]["progress"][0]["status"], "completed")
                return f.tool("get_evidence", {"info_unit_ids": ["U036"], "question_ids": ["facts"]}, "remaining-source")
            return f.finish(goal, ["U029", "U036"] if turn == 8 else ["U029"],
                refs=[("U029", 0), ("U036", 0)] if turn == 8 else [("U029", 0)],
                relations=["R010"] if turn == 6 else [], status="evidence_insufficient", call_id=f"finish-{turn}",
                updates=[] if turn == 8 else [self.blocked()])
        result, calls = self.run_script(goal, decide, case=self.claims_case(), work_context=deepcopy(self.context), issue_key=self.case.issue_set[1])
        self.assertEqual(len(calls), 8)
        self.assertEqual(result["stop_reason"], "finish_investigation")
        self.assertEqual(result["summary_status"], "completed")
        self.assertEqual(len(result["proposals"]), 2)
        self.assertEqual(result["tool_budget_used"]["finish"], 2)
        self.assertEqual(self.audits()[0]["tool_calls"][6]["result"]["status"], "invalid_request")
        self.assertTrue(all(len([m for m in c["messages"] if m["role"] == "system"]) == 1 for c in calls))

    def test_repeated_invalid_deliveries_never_publish_answer_or_findings(self):
        result, _ = self.run_script("核对", [f.finish("核对", ["U029"], refs=[("U029", 0)],
            answer="字" * (241 + i), call_id=str(i)) for i in range(3)], selected=["U029"])
        self.assertEqual(result["stop_reason"], "no_progress_limit")
        self.assertEqual(result["summary_status"], "failed")
        self.assertIsNone(result["answer"])
        self.assertEqual(result["findings"], [])

    def test_multiple_summary_violations_are_reported_together(self):
        from app.reasoning_models import ReasoningProposal
        text = "需要核对实际收款凭证才能确定候选解释是否得到支持以及是否存在空置租金损失"
        proposal = ReasoningProposal.model_validate({**suggestion("hypothesis"), "content": text})
        with self.assertRaises(AppError) as caught:
            agent._validate_summary(text + "字" * 241, [proposal])
        self.assertEqual({e["code"] for e in caught.exception.details["errors"]}, {"summary_too_long", "proposal_repeated"})

    def test_missing_finish_fields_are_reported_together(self):
        with self.assertRaises(AppError) as caught:
            agent._arguments("finish_investigation", '{"question_updates": [], "unexpected": true}')
        errors = caught.exception.details["errors"]
        self.assertEqual({tuple(e["loc"]) for e in errors}, {("answer",), ("findings",), ("step_updates",), ("completion_status",), ("<extra>",)})
        self.assertTrue(all(e["message"] and e["fix"] for e in errors))

    def test_question_material_alone_does_not_complete_unassociated_step(self):
        case, knowledge, questions, _seen = self.state(ids=("U029",))
        questions.update_steps([f.step(objective="核对入住日期")], case, knowledge)
        before = questions.dump()
        with self.assertRaises(AppError) as caught:
            questions.update_steps([f.step(objective="核对入住日期", status="completed", note="已处理")], case, knowledge)
        self.assertEqual(caught.exception.code, "step_completion_without_basis")
        self.assertEqual(questions.dump(), before)

    def test_explicit_basis_must_be_obtained_current_case_and_question_scoped(self):
        for mode in ("other_question", "unobtained", "other_case", "duplicate"):
            with self.subTest(mode=mode):
                case, knowledge, questions, seen = self.state(ids=("U029",) if mode == "unobtained" else ("U029", "U036"))
                questions.items["facts"].related_info_unit_ids = ["U029"]
                questions.update([f.question("另一方陈述", "Q2", units=["U036"])] if mode != "unobtained" else [f.question("另一方陈述", "Q2")], case, seen, self.context)
                basis = ["U036"]
                if mode == "other_case":
                    knowledge.info_units["foreign"] = {"id": "foreign"}
                    questions.items["facts"].related_info_unit_ids.append("foreign")
                    basis = ["foreign"]
                elif mode == "duplicate":
                    basis = ["U029", "U029"]
                elif mode == "unobtained":
                    questions.items["facts"].related_info_unit_ids.append("U036")
                before = questions.dump()
                with self.assertRaises(AppError) as caught:
                    questions.update_steps([f.step(status="completed", note="核对指定材料", basis=basis)], case, knowledge)
                self.assertEqual(caught.exception.code, "duplicate_step_basis" if mode == "duplicate" else "invalid_step_basis")
                self.assertEqual(questions.dump(), before)

    def test_valid_explicit_initial_basis_is_stored_without_redundant_reads(self):
        case, knowledge, questions, _seen = self.state(ids=("U029",))
        questions.update_steps([f.step(status="completed", note="核对已提供的陈述，不等于独立证实", basis=["U029"])], case, knowledge)
        step = questions.steps["S1"]
        self.assertEqual(step.basis_info_unit_ids, ["U029"])
        self.assertEqual(step.activity_indices, [])
        self.assertEqual(knowledge.activities, [])

    def test_empty_read_is_valid_but_failed_or_fabricated_activity_is_not_basis(self):
        for mode in ("empty", "failed", "not_executed", "wrong_step", "wrong_question"):
            with self.subTest(mode=mode):
                case, knowledge, questions, _seen = self.state(ids=("U029",))
                questions.update_steps([f.step()], case, knowledge)
                result = tools.search_info_units(case, "保险赔付")
                if mode == "failed":
                    result = tools._failure(AppError("source_unavailable", "模拟来源失败"))
                index = knowledge.merge("search_info_units", {"query": "保险赔付"}, result,
                    ["Q2"] if mode == "wrong_question" else ["facts"], step_id="other" if mode == "wrong_step" else "S1",
                    executed=mode != "not_executed")
                questions.record_step(questions.steps["S1"], index)
                row = f.step(status="completed", note="本查询未命中，不等于案件不存在该事实")
                if mode == "empty":
                    questions.update_steps([row], case, knowledge)
                    self.assertEqual(knowledge.activities[index]["arguments"]["query"], "保险赔付")
                    self.assertEqual(questions.steps["S1"].activity_indices, [index])
                else:
                    with self.assertRaises(AppError) as caught:
                        questions.update_steps([row], case, knowledge)
                    self.assertEqual(caught.exception.code, "step_completion_without_basis")

    def test_changed_objective_excludes_old_reads_but_keeps_history(self):
        case, knowledge, questions, _seen = self.state(ids=("U029",))
        questions.update_steps([f.step()], case, knowledge)
        index = knowledge.merge("search_info_units", {"query": "损失"}, tools.search_info_units(case, "损失"), ["facts"], step_id="S1")
        questions.record_step(questions.steps["S1"], index)
        questions.update_steps([f.step(status="in_progress", objective="核对日期")], case, knowledge)
        with self.assertRaises(AppError):
            questions.update_steps([f.step(status="completed", objective="核对日期", note="完成")], case, knowledge)
        self.assertEqual(questions.steps["S1"].activity_indices, [index])
        self.assertEqual(questions.steps["S1"].activity_start_index, 1)
        questions.update_steps([f.step(status="completed", objective="核对日期", note="明确重新选择已有材料，相关性仍需评审", basis=["U029"])], case, knowledge)
        self.assertEqual(questions.steps["S1"].basis_info_unit_ids, ["U029"])

    def test_finish_step_updates_and_question_updates_roll_back_on_any_delivery_error(self):
        for failure in ("invalid_basis", "missing_reason", "forged_activity", "summary", "citation", "container"):
            with self.subTest(failure=failure):
                case, knowledge, questions, seen = self.state(ids=("U029",))
                questions.update_steps([f.step()], case, knowledge)
                updates = [f.step(status="completed", note="核对已取得陈述", basis=["U029"])]
                args = self.finish_args(updates=[self.blocked()])
                if failure == "invalid_basis":
                    updates[0]["basis_info_unit_ids"] = ["U036"]
                elif failure == "missing_reason":
                    updates[0] = f.step(status="deferred")
                elif failure == "forged_activity":
                    updates[0]["activity_indices"] = [0]
                elif failure == "summary":
                    args["answer"] = "字" * 241
                elif failure == "citation":
                    args["findings"] = [{"statement": "核对材料", "info_unit_ids": ["U029"], "relation_ids": [],
                                         "evidence_refs": [{"info_unit_id": "U029", "source_ref_index": 99}]}]
                elif failure == "container":
                    updates = False
                args["step_updates"] = updates
                before, before_knowledge = questions.dump(), knowledge.dump()
                with self.assertRaises(AppError) as caught:
                    actions.finish_investigation(case, seen, self.context, "核对", [], questions, knowledge=knowledge, **args)
                details = caught.exception.details
                self.assertFalse(details["step_updates_applied"])
                self.assertFalse(details["question_updates_applied"])
                self.assertEqual(questions.dump(), before)
                self.assertEqual(knowledge.dump(), before_knowledge)
                if failure in {"invalid_basis", "missing_reason", "forged_activity", "container"}:
                    self.assertTrue(any(e.get("argument_loc", [None])[0] == "step_updates" for e in details["errors"]))
                args.update(answer="陈述尚缺独立佐证。", findings=[], step_updates=[f.step(status="completed", note="已处理所列材料", basis=["U029"])])
                result, draft, _ = actions.finish_investigation(case, seen, self.context, "核对", [], questions, knowledge=knowledge, **args)
                self.assertEqual(result.completion_status, "evidence_insufficient")
                self.assertEqual(draft.steps["S1"].status, "completed")
                self.assertEqual(draft.items["facts"].status, "blocked")

    def test_finish_deferral_cannot_answer_and_does_not_hide_other_pending_steps(self):
        case, knowledge, questions, seen = self.state(ids=("U029",))
        questions.update_steps([f.step(), f.step("S2")], case, knowledge)
        args = self.finish_args(updates=[self.blocked()])
        args["step_updates"] = [f.step(status="deferred", note="暂缺独立凭证")]
        before = questions.dump()
        with self.assertRaises(AppError) as caught:
            actions.finish_investigation(case, seen, self.context, "核对", [], questions, knowledge=knowledge, **args)
        self.assertIn("step_unresolved", {e["code"] for e in caught.exception.details["errors"]})
        self.assertEqual(questions.dump(), before)
        answered = f.question("核对", status="answered", units=["U029"], finding="已处理")
        args.update(question_updates=[answered], step_updates=[f.step(status="deferred", note="暂不处理"), f.step("S2", status="deferred", note="暂不处理")])
        with self.assertRaises(AppError) as caught:
            actions.finish_investigation(case, seen, self.context, "核对", [], questions, knowledge=knowledge, **args)
        self.assertIn("all_steps_deferred", {e["code"] for e in caught.exception.details["errors"]})
        self.assertEqual(questions.dump(), before)
