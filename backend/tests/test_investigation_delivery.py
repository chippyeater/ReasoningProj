"""Final delivery error-chain regressions; native tools, no model/network calls."""
from copy import deepcopy
import json
import unittest

import test_investigation_agent as f
import test_investigation_sufficiency as sufficiency
from test_reasoning_proposals import proposal_call, suggestion
from app import investigation_actions as actions, investigation_agent as agent
from app.errors import AppError


class TestInvestigationDelivery(unittest.TestCase):
    setUp = f.TestInvestigationAgent.setUp
    tearDown = f.TestInvestigationAgent.tearDown
    run_script = f.TestInvestigationAgent.run_script
    audits = f.TestInvestigationAgent.audits
    claims_case = sufficiency.TestInvestigationSufficiency.claims_case
    state = sufficiency.TestInvestigationSufficiency.state
    finish_args = sufficiency.TestInvestigationSufficiency.finish_args
    context = {"reasoning": {"hypotheses": [], "gaps": []}}

    def proposal(self, case, knowledge, seen, proposals, *, kind="hypothesis", qids=("Q1",), units=("U029",), **extra):
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
        questions.update_steps([f.step(), f.step("S2")], knowledge)
        questions.update_steps([f.step(status="completed", objective="检查上下文已有陈述", note="已检查，但尚无独立佐证")], knowledge)
        self.assertEqual(list(questions.steps), ["S1", "S2"])
        with self.assertRaises(AppError) as caught:
            actions.finish_investigation(case, seen, self.context, "核对", [], questions,
                **self.finish_args(updates=[self.blocked()]))
        self.assertIn("step_unresolved", {e["code"] for e in caught.exception.details["errors"]})
        questions.update_steps([f.step("S2", status="deferred", note="当前材料不足以核验独立收款")], knowledge)
        result, draft, _ = actions.finish_investigation(case, seen, self.context, "核对", [], questions,
            **self.finish_args(updates=[]))
        self.assertEqual(result.proposals, [])
        self.assertEqual(draft.steps["S1"].status, "completed")
        self.assertEqual(draft.steps["S2"].status, "deferred")

    def test_step_scope_terminal_history_and_forged_activity_rejected_atomically(self):
        from pydantic import ValidationError
        case, knowledge, questions, seen = self.state()
        questions.update([f.question("另一问题", "Q2")], case, seen, self.context)
        questions.update_steps([f.step(status="completed", note="已处理上下文材料")], knowledge)
        before = questions.dump()
        for row in (f.step(qid="Q2", status="completed", note="换问题"),
                    f.step(status="pending"), f.step(status="completed", objective="改为别的目标", note="换目标"),
                    {**f.step(), "activity_indices": [0]},
                    f.step("S2", qid="unknown"), f.step("S2", status="deferred")):
            with self.subTest(row=row), self.assertRaises((AppError, ValidationError)):
                questions.update_steps([f.step("new"), row], knowledge)
            self.assertEqual(questions.dump(), before)
        questions.update_steps([f.step(status="in_progress", note="重新核查")], knowledge)
        self.assertEqual(questions.steps["S1"].status, "in_progress")
        self.assertEqual(questions.steps["S1"].activity_indices, [])

    def test_question_updates_cannot_delete_registered_steps(self):
        case, knowledge, questions, seen = self.state()
        questions.update_steps([f.step()], knowledge)
        questions.update([self.blocked()], case, seen, self.context)
        self.assertIn("S1", questions.steps)
        with self.assertRaises(AppError) as caught:
            actions.finish_investigation(case, seen, self.context, "核对", [], questions,
                **self.finish_args(updates=[]))
        self.assertEqual(caught.exception.details["errors"][0]["code"], "step_unresolved")

    def test_question_has_no_delivery_plan_and_rejects_removed_field(self):
        case, knowledge, questions, seen = self.state()
        self.assertNotIn("planned_actions", questions.items["Q1"].model_dump())
        with self.assertRaises(AppError) as caught:
            questions.update([{**f.question("核对"), "planned_actions": []}], case, seen, self.context)
        self.assertEqual(caught.exception.details["errors"][0]["code"], "extra_forbidden")
        self.assertEqual(questions.items["Q1"].status, "pending")

    def test_shared_hypothesis_dependency_keeps_real_question_scope(self):
        case, knowledge, questions, seen = self.state()
        proposals = []
        questions.update([f.question("检验共同假设", "Q2")], case, seen, self.context)
        hid = self.proposal(case, knowledge, seen, proposals, qids=("Q2",))
        gid = self.proposal(case, knowledge, seen, proposals, kind="evidence_gap")
        questions.update([self.blocked()], case, seen, self.context)
        self.assertEqual(proposals[1].hypothesis_proposal_ids, [hid])
        self.assertEqual([(a["proposal_id"], a["question_ids"]) for a in knowledge.activities], [(hid, ["Q2"]), (gid, ["Q1"])])
        self.assertEqual(questions.steps, {})

    def test_proposal_unknown_question_cannot_create_or_reassign_suggestion(self):
        source = suggestion("hypothesis")
        source.update(info_unit_ids=[], evidence_refs=[])
        reply = proposal_call(source, [], "foreign-question")
        call = reply.message["tool_calls"][0]
        args = json.loads(call["function"]["arguments"])
        args["question_ids"] = ["foreign"]
        result, calls = self.run_script("核对", [f.tool("propose_hypothesis", args, "invalid"), f.finish("核对")],
            work_context=deepcopy(self.context), issue_key=self.case.issue_set[1])
        self.assertEqual(json.loads(calls[1]["messages"][-1]["content"])["error"]["code"], "invalid_question_reference")
        self.assertEqual(result["proposals"], [])

    def test_first_finish_reports_step_question_relation_source_and_summary_errors(self):
        case, knowledge, questions, seen = self.state(ids=("U029",))
        proposals = []
        self.proposal(case, knowledge, seen, proposals)
        self.proposal(case, knowledge, seen, proposals, kind="evidence_gap")
        questions.update_steps([f.step()], knowledge)
        updates = [{**self.blocked(), "question": "非法缩减原始任务"}]
        args = self.finish_args(updates=updates)
        args.update(answer="字" * 241, findings=[{"statement": "两方陈述待核实", "info_unit_ids": ["U029"],
            "relation_ids": ["R010"], "evidence_refs": [{"info_unit_id": "U029", "source_ref_index": 99}]}])
        before = questions.dump()
        with self.assertRaises(AppError) as caught:
            actions.finish_investigation(case, seen, self.context, "核对", proposals, questions, **args)
        details = caught.exception.details
        codes = {e["code"] for e in details["errors"]}
        self.assertTrue({"step_unresolved", "goal_scope_changed", "reference_not_obtained", "reference_out_of_range", "summary_too_long"} <= codes)
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
            actions.finish_investigation(case, seen, self.context, "核对", [], questions, **args)
        codes = {e["code"] for e in caught.exception.details["errors"]}
        self.assertTrue({"missing", "model_type", "reference_not_found", "summary_too_long"} <= codes)
        self.assertTrue(any(e.get("argument_loc") == ["question_updates", 1, "question"] for e in caught.exception.details["errors"]))
        self.assertEqual(questions.items["Q1"].status, "pending")

    def test_validated_question_batch_survives_failed_delivery_and_empty_updates_reuse_it(self):
        def decide(call, turn):
            if turn == 1:
                return f.finish("核对", ["U029"], refs=[("U029", 0)], answer="字" * 241, call_id="bad-answer")
            state = json.loads(call["messages"][1]["content"])
            error = json.loads(call["messages"][-1]["content"])["error"]
            self.assertTrue(error["validation_details"]["question_updates_applied"])
            self.assertEqual(state["question_state"]["progress"][0]["status"], "answered")
            self.assertEqual(state["knowledge_state"]["findings"]["items"], [])
            return f.finish("核对", ["U029"], refs=[("U029", 0)], updates=[], answer="已核对陈述，仍需独立核实。")
        result, calls = self.run_script("核对", decide, selected=["U029"])
        self.assertEqual(result["stop_reason"], "finish_investigation")
        self.assertEqual(len(calls), 2)

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
                self.assertEqual(state["investigation_steps"]["progress"][0]["activity_indices"], [1])
                self.assertEqual(state["investigation_steps"]["progress"][0]["status"], "completed")
                return f.tool("get_evidence", {"info_unit_ids": ["U036"]}, "remaining-source")
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
        self.assertEqual(self.audits()[0]["tool_calls"][5]["result"]["status"], "invalid_request")
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
        self.assertEqual({tuple(e["loc"]) for e in errors}, {("answer",), ("findings",), ("completion_status",), ("<extra>",)})
        self.assertTrue(all(e["message"] and e["fix"] for e in errors))
