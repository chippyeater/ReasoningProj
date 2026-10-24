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

    def plan(self, pid, kind="hypothesis", ident="H", description="登记可检验的租金损失解释"):
        return {"id": ident, "kind": kind, "description": description, "proposal_id": pid, "omission_reason": None}

    def blocked(self, plans=(), units=("U029",), skipped=()):
        return f.question("核对", status="blocked", units=list(units), reason="缺独立收款凭证", block_kind="evidence_missing",
                          plans=list(plans), skipped_leads=list(skipped))

    def test_paraphrased_plans_link_hypothesis_and_gap_by_ids_and_provenance(self):
        case, knowledge, questions, seen = self.state()
        proposals = []
        hid = self.proposal(case, knowledge, seen, proposals)
        gid = self.proposal(case, knowledge, seen, proposals, kind="evidence_gap", content="核查收款账目与计租日")
        plans = [self.plan(hid), self.plan(gid, "evidence_gap", "G", "补充实际收款证明")]
        questions.update([f.question("核对", plans=[{**p, "proposal_id": None} for p in plans])], case, seen, self.context, proposals, knowledge)
        plans[0]["description"] = "保留尚待核实的候选解释"  # Wording is not identity.
        result, draft, _ = actions.finish_investigation(case, seen, self.context, "核对", proposals, questions,
            knowledge=knowledge, **self.finish_args(updates=[self.blocked(plans)]))
        self.assertEqual(len(result.proposals), 2)
        self.assertEqual([p.proposal_id for p in draft.items["Q1"].planned_actions], [hid, gid])

    def test_wrong_kind_foreign_run_cross_question_and_disjoint_basis_rejected(self):
        for mode in ("kind", "foreign_run", "question", "material"):
            with self.subTest(mode=mode):
                case, knowledge, questions, seen = self.state()
                proposals = []
                pid = self.proposal(case, knowledge, seen, proposals, qids=("Q2",) if mode == "question" else ("Q1",),
                                    units=("U036",) if mode == "material" else ("U029",))
                plan = self.plan("proposal-" + "f" * 32 if mode == "foreign_run" else pid,
                                 "evidence_gap" if mode == "kind" else "hypothesis")
                before = questions.dump()
                with self.assertRaises(AppError) as caught:
                    questions.update([self.blocked([plan])], case, seen, self.context, proposals, knowledge)
                self.assertEqual(caught.exception.code, "invalid_plan_reference")
                self.assertEqual(caught.exception.details["errors"][0]["loc"], ["questions", "Q1", "planned_actions", "H", "proposal_id"])
                self.assertEqual(questions.dump(), before)

    def test_one_proposal_cannot_discharge_two_plans_or_replace_valid_binding(self):
        case, knowledge, questions, seen = self.state()
        proposals = []
        pid = self.proposal(case, knowledge, seen, proposals)
        other = self.proposal(case, knowledge, seen, proposals, content="另一项候选解释")
        questions.update([self.blocked([self.plan(pid)])], case, seen, self.context, proposals, knowledge)
        before = questions.dump()
        for plans in ([self.plan(pid), self.plan(pid, ident="H2")], [self.plan(other)]):
            with self.subTest(plans=plans), self.assertRaises(AppError):
                questions.update([self.blocked(plans)], case, seen, self.context, proposals, knowledge)
            self.assertEqual(questions.dump(), before)

    def test_shared_hypothesis_dependency_across_questions_remains_valid(self):
        case, knowledge, questions, seen = self.state()
        proposals = []
        questions.update([f.question("检验共同假设", "Q2")], case, seen, self.context, proposals, knowledge)
        self.proposal(case, knowledge, seen, proposals, qids=("Q2",))
        gid = self.proposal(case, knowledge, seen, proposals, kind="evidence_gap")
        questions.update([self.blocked([self.plan(gid, "evidence_gap", "G")])], case, seen, self.context, proposals, knowledge)
        self.assertEqual(questions.items["Q1"].planned_actions[0].proposal_id, gid)

    def test_first_finish_reports_plan_coverage_relation_source_and_summary_errors(self):
        case, knowledge, questions, seen = self.state(ids=("U029",))
        proposals = []
        hid = self.proposal(case, knowledge, seen, proposals)
        self.proposal(case, knowledge, seen, proposals, kind="evidence_gap")
        updates = [self.blocked([self.plan(hid, "evidence_gap")])]
        args = self.finish_args(updates=updates)
        args.update(answer="字" * 241, findings=[{"statement": "两方陈述待核实", "info_unit_ids": ["U029"],
            "relation_ids": ["R010"], "evidence_refs": [{"info_unit_id": "U029", "source_ref_index": 99}]}])
        before = questions.dump()
        with self.assertRaises(AppError) as caught:
            actions.finish_investigation(case, seen, self.context, "核对", proposals, questions, knowledge=knowledge, **args)
        details = caught.exception.details
        codes = {e["code"] for e in details["errors"]}
        self.assertTrue({"invalid_plan_reference", "uninvestigated_lead", "reference_not_obtained", "reference_out_of_range", "summary_too_long"} <= codes)
        self.assertEqual(details["errors"][0]["argument_loc"], ["question_updates", 0, "planned_actions", 0, "proposal_id"])
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
        self.assertTrue({"missing", "model_type", "uninvestigated_lead", "reference_not_found", "summary_too_long"} <= codes)
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

    def test_native_error_chain_recovers_after_one_feedback_without_extra_summary_call(self):
        goal = "核对"
        def decide(call, turn):
            if turn == 1:
                return f.tool("get_evidence", {"info_unit_ids": ["U029"]}, "read")
            state = json.loads(call["messages"][1]["content"])
            ids = [p["proposal_id"] for p in state["knowledge_state"]["proposals"]["items"]]
            if turn == 2:
                return proposal_call(suggestion("hypothesis"), [], "hypothesis")
            if turn == 3:
                return proposal_call({**suggestion("evidence_gap"), "hypothesis_proposal_ids": ids}, [], "gap")
            if turn == 5:
                error = json.loads(call["messages"][-1]["content"])["error"]
                codes = {e["code"] for e in error["validation_details"]["errors"]}
                self.assertTrue({"uninvestigated_lead", "reference_not_obtained"} <= codes)
                self.assertNotIn("invalid_plan_reference", codes)
                self.assertTrue(error["validation_details"]["question_updates_applied"])
                self.assertEqual(state["question_state"]["details"]["items"][0]["planned_actions"][0]["proposal_id"], ids[0])
                return f.tool("get_evidence", {"info_unit_ids": ["U036"]}, "remaining-source")
            plans = [self.plan(ids[0]), self.plan(ids[1], "evidence_gap", "G", "核查计租和收款凭据")]
            return f.finish(goal, ["U029", "U036"] if turn == 6 else ["U029"],
                refs=[("U029", 0), ("U036", 0)] if turn == 6 else [("U029", 0)],
                relations=["R010"] if turn == 4 else [], status="evidence_insufficient", call_id=f"finish-{turn}",
                updates=[] if turn == 6 else [self.blocked(plans)])
        result, calls = self.run_script(goal, decide, case=self.claims_case(), work_context=deepcopy(self.context), issue_key=self.case.issue_set[1])
        self.assertEqual(len(calls), 6)
        self.assertEqual(result["stop_reason"], "finish_investigation")
        self.assertEqual(result["summary_status"], "completed")
        self.assertEqual(len(result["proposals"]), 2)
        self.assertEqual(result["tool_budget_used"]["finish"], 2)
        self.assertEqual(self.audits()[0]["tool_calls"][3]["result"]["status"], "invalid_request")
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
