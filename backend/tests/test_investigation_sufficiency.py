"""Completion contracts and audit-only discovery hints, never read quotas."""
import json
import unittest
from unittest.mock import patch

import test_investigation_agent as f
from app import investigation_actions as actions, investigation_agent as agent, investigation_tools as tools
from app.errors import AppError
from app.investigation_state import KnowledgeState, QuestionState, model_context


class TestInvestigationSufficiency(unittest.TestCase):
    setUp = f.TestInvestigationAgent.setUp
    tearDown = f.TestInvestigationAgent.tearDown
    run_script = f.TestInvestigationAgent.run_script
    audits = f.TestInvestigationAgent.audits

    def claims_case(self):
        # Minimal two-source scenario; omitted SourceRefs are tested separately.
        case = self.case.model_copy(deep=True)
        case.info_units = [u for u in case.info_units if u.id in {"U029", "U036"}]
        for unit in case.info_units:
            unit.source_refs = unit.source_refs[:1]
        case.relations = [r for r in case.relations if r.id == "R010"]
        return case

    def state(self, goal="核对", ids=("U029", "U036")):
        case = self.claims_case()
        initial = {"goal": goal, "selected_info_units": [tools._summary(u) for u in case.info_units if u.id in ids],
            "evidence_context": tools.initial_evidence_context(case, list(ids))}
        knowledge, questions = KnowledgeState(initial), QuestionState(goal)
        questions.items["Q1"].related_info_unit_ids = list(ids)
        seen = {"units": set(ids), "relations": set(), "evidence": {
            (r["info_unit_id"], r["source_ref_index"]) for r in initial["evidence_context"]["items"]}}
        return case, knowledge, questions, seen

    def finish_args(self, goal="核对", *, status="evidence_insufficient", updates=None):
        return agent._arguments("finish_investigation", f.finish(goal, status=status, updates=updates).message["tool_calls"][0]["function"]["arguments"])

    def test_unread_suggestions_do_not_block_valid_insufficient_finish(self):
        goal = "核对"
        result, calls = self.run_script(goal, [f.finish(goal, ["U029"], refs=[("U029", 0)],
            status="evidence_insufficient", questions=["缺少独立佐证"])], selected=["U029"], case=self.claims_case())
        state = json.loads(calls[0]["messages"][1]["content"])
        self.assertNotIn("investigation_opportunities", state)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["stop_reason"], "finish_investigation")
        self.assertEqual(result["completion_status"], "evidence_insufficient")
        self.assertTrue(result["open_questions"])
        self.assertEqual(result["tool_budget_used"]["read"], 0)
        audit = self.audits()[0]
        self.assertEqual(audit["tool_calls"][0]["result"]["status"], "ok")
        self.assertNotIn("U036", audit["knowledge_state"]["info_units"])
        self.assertTrue(audit["investigation_opportunities"]["Q1"]["items"])

    def test_audit_candidates_never_enter_model_requests_including_error_recovery(self):
        marker = "AUDIT_ONLY_CANDIDATE_NEVER_SENT_TO_MODEL"
        with patch.object(KnowledgeState, "available_work", return_value={"Q1": {"items": [marker]}}):
            result, calls = self.run_script("核对", [
                f.tool("propose_hypothesis", {"content": "待检验解释", "rationale": "待核实",
                    "info_unit_ids": ["U036"], "evidence_refs": []}, "invalid"),
                f.tool("get_evidence", {"info_unit_ids": ["U036"]}, "read"),
                f.finish("核对", ["U029", "U036"], refs=[("U029", 0), ("U036", 0)],
                    status="evidence_insufficient")], selected=["U029"], case=self.claims_case())
        self.assertEqual(result["stop_reason"], "finish_investigation")
        for call in calls:
            request = json.dumps({"messages": call["messages"], "tools": call["definitions"]})
            self.assertNotIn("investigation_opportunities", request)
            self.assertNotIn(marker, request)
        self.assertEqual(json.loads(calls[1]["messages"][-1]["content"])["status"], "invalid_request")
        self.assertEqual(self.audits()[0]["investigation_opportunities"]["Q1"]["items"], [marker])

    def test_actual_insufficiency_can_finish_from_complete_initial_material_without_reads(self):
        result, calls = self.run_script("核对", [f.finish("核对", ["U029", "U036"], refs=[("U029", 0), ("U036", 0)],
            status="evidence_insufficient", questions=["缺少独立佐证，不能把单方陈述当作核实结果"])],
            selected=["U029", "U036"], case=self.claims_case())
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["tool_budget_used"]["read"], 0)
        self.assertEqual(result["question_state"]["questions"][0]["status"], "blocked")

    def test_simple_fact_can_finish_immediately_even_if_other_material_exists(self):
        result, calls = self.run_script("笔录中称何时入住", [f.finish("笔录中称何时入住", ["U026"], refs=[("U026", 0)],
            status="completed", statement="据张某陈述，3 月 8 日入住；这是陈述内容。")], selected=["U026"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["tool_budget_used"]["read"], 0)
        self.assertEqual(result["completion_status"], "completed")

    def test_multiple_explicit_answers_can_all_be_delivered_in_one_finish_call(self):
        goal = "两方笔录各自怎么说"
        result, calls = self.run_script(goal, [f.finish(goal, ["U029", "U036"], refs=[("U029", 0), ("U036", 0)],
            status="completed", updates=[
                f.question(goal, status="answered", units=["U029", "U036"], finding="已分别报告两方陈述，不判断其真假"),
                f.question("张某笔录说什么", "tenant", status="answered", units=["U029"], refs=[("U029", 0)], finding="张某认为没有实际损失"),
                f.question("李某笔录说什么", "landlord", status="answered", units=["U036"], refs=[("U036", 0)], finding="李某声称有空置损失")])],
            selected=["U029", "U036"], case=self.claims_case())
        self.assertTrue(all(q["status"] == "answered" for q in result["question_state"]["questions"]))
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["open_questions"], [])

    def test_all_answered_cannot_be_called_evidence_insufficient(self):
        case, knowledge, questions, seen = self.state()
        answered = f.question("核对", status="answered", units=["U029"], finding="双方陈述有分歧，不能确定")
        with self.assertRaises(AppError) as caught:
            actions.finish_investigation(case, seen, None, "核对", [], questions, knowledge=knowledge,
                **self.finish_args(updates=[answered]))
        self.assertEqual(caught.exception.details["errors"][0]["code"], "insufficiency_without_unresolved_question")
        # The Question basis is valid working memory; the inconsistent final
        # completion_status still prevents any delivery.
        self.assertEqual(questions.items["Q1"].status, "answered")

    def test_multi_goal_rollup_requires_each_requested_aspect_to_be_resolved(self):
        goal = "有无损失，以及损失金额是多少"
        case, knowledge, questions, seen = self.state(goal)
        questions.update([f.question("有无损失", "existence"), f.question("损失金额", "amount")], case, seen, None, [])
        updates = [f.question(goal, status="answered", units=["U029"], finding="已处理存在性"),
            f.question("有无损失", "existence", status="answered", units=["U029"], finding="材料仅为单方判断"),
            f.question("损失金额", "amount", status="blocked", reason="未提供净额计算条件", block_kind="evidence_missing")]
        with self.assertRaises(AppError) as caught:
            actions.finish_investigation(case, seen, None, goal, [], questions, knowledge=knowledge,
                **self.finish_args(goal, updates=updates))
        self.assertEqual(caught.exception.details["errors"][0]["code"], "goal_scope_incomplete")
        self.assertEqual(questions.items["amount"].status, "pending")

    def test_root_goal_cannot_be_narrowed_to_drop_a_requested_aspect(self):
        goal = "核对日期，并查明金额"
        case, _knowledge, questions, seen = self.state(goal)
        with self.assertRaises(AppError) as caught:
            questions.update([f.question("只核对日期")], case, seen, None, [])
        self.assertEqual(caught.exception.code, "goal_scope_changed")
        self.assertEqual(questions.items["Q1"].question, goal)

    def test_finish_does_not_consult_the_discovery_index(self):
        case, knowledge, questions, seen = self.state(ids=("U029",))
        blocked = f.question("核对", status="blocked", reason="缺独立佐证", block_kind="evidence_missing")
        with patch.object(knowledge, "available_work", side_effect=AssertionError("Advice is not a completion dependency")):
            result, updated, _answer = actions.finish_investigation(case, seen, None, "核对", [], questions,
                knowledge=knowledge, **self.finish_args(updates=[blocked]))
        self.assertEqual(result.completion_status, "evidence_insufficient")
        self.assertEqual(updated.items["Q1"].status, "blocked")
        self.assertNotIn("U036", seen["units"])
        self.assertEqual(knowledge.activities, [])

    def test_removed_skip_field_is_absent_from_schema_and_not_accepted(self):
        case, knowledge, questions, seen = self.state(ids=("U029",))
        self.assertNotIn("skipped_leads", json.dumps(agent.tool_definitions()))
        self.assertNotIn("LeadAssessment", json.dumps(agent.tool_definitions()))
        self.assertNotIn("skipped_leads", questions.items["Q1"].model_fields)
        update = {**f.question("核对", status="blocked", reason="缺佐证", block_kind="evidence_missing"), "skipped_leads": []}
        with self.assertRaises(AppError) as caught:
            questions.update([update], case, seen, None, [], knowledge)
        self.assertTrue(any(e["code"] == "extra_forbidden" and e["loc"][-1] == "skipped_leads"
                            for e in caught.exception.details["errors"]))
        self.assertEqual(questions.items["Q1"].status, "pending")

    def test_unread_source_and_truncated_query_remain_explicit_after_compaction(self):
        case, knowledge, questions, seen = self.state(ids=("U029",))
        knowledge.source_refs["U029:0"]["quote_truncated"] = True
        knowledge.merge("search_info_units", {"query": "押金", "limit": 1}, tools._result([], has_more=True), ["Q1"])
        before = knowledge.dump()
        opportunities = knowledge.available_work(case, questions)
        ids = {item["lead_id"] for item in opportunities["Q1"]["items"]}
        self.assertTrue({"source:U029", "query:0"} <= ids)
        view = model_context({"goal": "核对", "selected_info_units": []}, knowledge, questions, {}, None)
        self.assertNotIn("investigation_opportunities", view)
        self.assertTrue(view["knowledge_state"]["source_refs"]["items"][0]["quote_truncated"])
        self.assertTrue(view["knowledge_state"]["activities"]["items"][0]["has_more"])
        self.assertEqual(knowledge.dump(), before)
        self.assertNotIn(("U036", 0), seen["evidence"])
        result, _, _ = actions.finish_investigation(case, seen, None, "核对", [], questions,
            knowledge=knowledge, **self.finish_args())
        self.assertEqual(result.completion_status, "evidence_insufficient")
        self.assertEqual(knowledge.dump(), before)

    def test_budget_completion_and_block_kind_still_require_exhausted_budget(self):
        case, knowledge, questions, seen = self.state(ids=("U029",))
        for completion in ("budget_exhausted", "evidence_insufficient"):
            args = self.finish_args(status="budget_exhausted")
            args["completion_status"] = completion
            with self.subTest(completion=completion), self.assertRaises(AppError) as caught:
                actions.finish_investigation(case, seen, None, "核对", [], questions,
                    knowledge=knowledge, **args)
            self.assertIn("budget_available", {e["code"] for e in caught.exception.details["errors"]})
        args = self.finish_args(status="budget_exhausted")
        result, _, _ = actions.finish_investigation(case, seen, None, "核对", [], questions,
            knowledge=knowledge, budget_exhausted=True, **args)
        self.assertEqual(result.completion_status, "budget_exhausted")

    def test_advisory_candidates_do_not_authorize_unread_finding_references(self):
        case, knowledge, questions, seen = self.state(ids=("U029",))
        self.assertTrue(knowledge.available_work(case, questions)["Q1"]["items"])
        args = agent._arguments("finish_investigation", f.finish("核对", ["U036"], refs=[("U036", 0)],
            status="evidence_insufficient").message["tool_calls"][0]["function"]["arguments"])
        with self.assertRaises(AppError) as caught:
            actions.finish_investigation(case, seen, None, "核对", [], questions, knowledge=knowledge,
                **args)
        self.assertIn("reference_not_obtained", {e["code"] for e in caught.exception.details["errors"]})
        self.assertNotIn("U036", seen["units"])
        self.assertEqual(questions.items["Q1"].status, "pending")

    def test_unread_suggestions_do_not_relax_blocked_completed_consistency(self):
        case, knowledge, questions, seen = self.state(ids=("U029",))
        blocked = f.question("核对", status="blocked", reason="缺独立佐证", block_kind="evidence_missing")
        with self.assertRaises(AppError) as caught:
            actions.finish_investigation(case, seen, None, "核对", [], questions, knowledge=knowledge,
                **self.finish_args(status="completed", updates=[blocked]))
        self.assertIn("blocked_questions", {e["code"] for e in caught.exception.details["errors"]})

    def test_discovery_index_does_not_authorize_unread_proposal_references(self):
        case, knowledge, questions, seen = self.state(ids=("U029",))
        self.assertTrue(knowledge.available_work(case, questions)["Q1"]["items"])
        proposals = []
        with self.assertRaises(AppError) as caught:
            actions.propose_hypothesis(case, seen, {"reasoning": {"hypotheses": [], "gaps": []}}, proposals,
                content="待检验解释", rationale="尚未读到此材料", info_unit_ids=["U036"], evidence_refs=[])
        self.assertEqual(caught.exception.details["errors"][0]["code"], "reference_not_obtained")
        self.assertEqual(proposals, [])

    def test_unprovided_source_can_be_obtained_without_reading_the_whole_case(self):
        case, knowledge, questions, seen = self.state()
        knowledge.source_refs["U029:0"]["quote_truncated"] = True
        self.assertIn("source:U029", {r["lead_id"] for r in knowledge.available_work(case, questions)["Q1"]["items"]})
        args = {"info_unit_ids": ["U029"]}
        response = tools.get_evidence(case, **args)
        knowledge.merge("get_evidence", args, response, ["Q1"])
        agent._remember("get_evidence", response, seen)
        self.assertNotIn("source:U029", {r["lead_id"] for r in knowledge.available_work(case, questions)["Q1"]["items"]})
