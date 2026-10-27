"""Discovery permissions, native read gating and actual acquisition at finish."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

import test_investigation_agent as f
import test_observability as tracing
from app import investigation_actions as actions, investigation_agent as agent, investigation_tools as tools, observability
from app.errors import AppError
from app.investigation_state import KnowledgeState, QuestionState, case_info_directory, model_context


class TestInvestigationAcquisition(unittest.TestCase):
    setUp = f.TestInvestigationAgent.setUp
    tearDown = f.TestInvestigationAgent.tearDown
    run_script = f.TestInvestigationAgent.run_script
    audits = f.TestInvestigationAgent.audits

    def insufficient(self, *, completed=False, reason="计租及收款条件未核实", kind="evidence_missing", status="evidence_insufficient"):
        return f.finish("房东是否实际产生损失？如果有，如何量化？", status=status,
            updates=[f.question("核对损失条件", status="blocked", reason=reason, block_kind=kind)],
            step_updates=[f.step(status="completed" if completed else "deferred", note=reason)])

    def test_directory_and_native_read_gate_then_full_tools_and_exact_source(self):
        goal = "房东是否实际产生损失？如果有，如何量化？"
        result, calls = self.run_script(goal, [f.plan(),
            f.tool("search_info_units", {"query": "月租", "step_id": "S1"}, "search"),
            f.tool("get_evidence", {"info_unit_ids": ["U004"], "step_id": "S1"}, "source"),
            self.insufficient(completed=True)])
        entry, first, full = [json.loads(c["messages"][1]["content"]) for c in calls[:3]]
        directory = entry["case_info_directory"]
        self.assertEqual(directory["total_count"], 17)
        self.assertEqual(len(directory["items"]), 17)
        self.assertEqual(directory["omitted_count"], 0)
        self.assertFalse(directory["context_truncated"])
        self.assertEqual(directory["obtained_info_unit_count"], 0)
        self.assertTrue(all(set(r) == {"id", "title", "type", "title_truncated"} for r in directory["items"]))
        self.assertEqual(entry["knowledge_state"]["info_units"]["items"], [])
        self.assertEqual(first["tool_policy"], "initial_read")
        self.assertEqual(calls[1]["tool_choice"], "required")
        self.assertEqual({d["function"]["name"] for d in calls[1]["definitions"]}, set(agent.READ_TOOLS))
        self.assertEqual(full["tool_policy"], "execution")
        self.assertEqual(len(calls[2]["definitions"]), 10)
        self.assertEqual(calls[2]["tool_choice"], "auto")
        self.assertEqual(full["case_info_directory"]["obtained_info_unit_count"], 1)
        self.assertEqual(full["knowledge_state"]["source_refs"]["items"], [])
        last = json.loads(calls[-1]["messages"][1]["content"])
        self.assertEqual(last["goal"], goal)
        self.assertEqual(last["knowledge_state"]["source_refs"]["items"][0]["info_unit_id"], "U004")
        self.assertEqual(last["question_state"]["details"]["items"][0]["evidence_refs"], [{"info_unit_id": "U004", "source_ref_index": 0}, {"info_unit_id": "U004", "source_ref_index": 1}])
        self.assertEqual(last["investigation_steps"]["progress"][0]["activity_indices"], [1, 2])
        self.assertEqual(result["completion_status"], "evidence_insufficient")
        self.assertEqual(self.audits()[0]["information_acquisition"]["successful_read_count"], 2)

    def test_hidden_finish_cannot_dispatch_or_consume_finish_budget(self):
        premature = self.insufficient()
        premature.message["tool_calls"][0]["id"] = "premature"
        result, calls = self.run_script("核对损失", [f.plan(), premature,
            f.tool("search_info_units", {"query": "不存在的账目", "step_id": "S1"}, "read"), self.insufficient(completed=True)])
        state = json.loads(calls[2]["messages"][1]["content"])
        self.assertEqual(state["recovery"]["code"], "initial_information_required")
        self.assertEqual(state["tool_policy"], "initial_read")
        self.assertEqual(state["remaining_budget"]["finish"], agent.MAX_FINISH_CALLS)
        self.assertEqual(state["question_state"]["progress"][0]["status"], "pending")
        self.assertEqual(result["tool_budget_used"]["finish"], 1)
        self.assertFalse(self.audits()[0]["tool_calls"][1]["executed"])

    def test_empty_and_truncated_reads_release_gate_without_granting_sources(self):
        for query, limit, expected in (("不存在的登记", 8, "read_empty"), ("押金", 1, "read_obtained")):
            with self.subTest(query=query):
                _, calls = self.run_script("核对损失", [f.plan(),
                    f.tool("search_info_units", {"query": query, "limit": limit, "step_id": "S1"}, "read"),
                    self.insufficient(completed=True)])
                state = json.loads(calls[-1]["messages"][1]["content"])
                self.assertEqual(state["information_acquisition"]["status"], expected)
                self.assertEqual(state["tool_policy"], "execution")
                self.assertEqual(state["knowledge_state"]["source_refs"]["items"], [])
                self.assertEqual(state["information_acquisition"]["last_read"]["has_more"], limit == 1)

    def test_bad_id_does_not_release_gate_but_real_source_error_does(self):
        failure = tools._failure(AppError("source_quote_mismatch", "Cached source unavailable"), status="error")
        actual = tools.get_evidence
        def read(case, info_unit_ids):
            return actual(case, info_unit_ids) if info_unit_ids == ["unknown"] else deepcopy(failure)
        with patch.object(tools, "get_evidence", side_effect=read):
            result, calls = self.run_script("核对损失", [f.plan(),
                f.tool("get_evidence", {"info_unit_ids": ["unknown"]}, "invalid"),
                f.tool("get_evidence", {"info_unit_ids": ["U004"], "step_id": "S1"}, "source"),
                self.insufficient(reason="来源缓存校验失败，需修复来源", kind="tool_failure")])
        self.assertEqual(json.loads(calls[2]["messages"][1]["content"])["tool_policy"], "initial_read")
        state = json.loads(calls[3]["messages"][1]["content"])
        self.assertEqual(state["tool_policy"], "execution")
        self.assertEqual(state["information_acquisition"]["status"], "read_failed")
        self.assertEqual(state["information_acquisition"]["read_error_count"], 1)
        self.assertEqual(result["completion_status"], "evidence_insufficient")

    def test_initial_question_material_can_bypass_gate_without_extra_reads(self):
        plan = f.tool("update_investigation_steps", {"questions": [f.question("核对", units=["U004"])], "steps": [f.step()]})
        _, calls = self.run_script("核对损失", [plan, self.insufficient()], selected=["U004"])
        state = json.loads(calls[1]["messages"][1]["content"])
        self.assertEqual(state["information_acquisition"]["status"], "initial_material")
        self.assertEqual(state["tool_policy"], "execution")
        self.assertEqual(len(calls[1]["definitions"]), 10)

    def test_empty_case_and_zero_read_budget_can_report_actual_limits(self):
        case = self.case.model_copy(deep=True)
        case.info_units, case.relations = [], []
        _, calls = self.run_script("核对损失", [f.plan(), self.insufficient()], case=case)
        self.assertEqual(json.loads(calls[1]["messages"][1]["content"])["information_acquisition"]["status"], "no_case_material")
        with patch.object(agent, "MAX_TOOL_CALLS", 0):
            result, calls = self.run_script("核对损失", [f.plan(), self.insufficient(
                reason="读取预算已耗尽", kind="budget_exhausted", status="budget_exhausted")])
        state = json.loads(calls[1]["messages"][1]["content"])
        self.assertEqual(state["information_acquisition"]["status"], "read_budget_exhausted")
        self.assertNotIn("search_info_units", {d["function"]["name"] for d in calls[1]["definitions"]})
        self.assertEqual(result["completion_status"], "budget_exhausted")

    def test_finish_rejects_uninvestigated_insufficiency_without_mutating_state(self):
        initial = {"selected_info_units": [], "evidence_context": {"items": []}}
        knowledge, questions = KnowledgeState(initial), QuestionState()
        seen = {"units": set(), "relations": set(), "evidence": set()}
        questions.submit_plan([f.question("核对损失")], [f.step()], self.case, seen, None, knowledge)
        before = questions.dump()
        args = json.loads(self.insufficient().message["tool_calls"][0]["function"]["arguments"])
        for exhausted in (False, True):  # Proposal exhaustion cannot substitute for read exhaustion.
            with self.assertRaises(AppError) as caught:
                actions.finish_investigation(self.case, seen, None, "核对损失", [], questions,
                    knowledge=knowledge, budget_exhausted=exhausted, **args)
            self.assertEqual(caught.exception.code, "initial_information_required")
            self.assertIn("Search or read", caught.exception.details["errors"][0]["fix"])
        self.assertEqual(questions.dump(), before)
        knowledge.merge("search_info_units", {"query": "不存在的记录"}, tools._result([]), ["facts"])
        result, draft, _ = actions.finish_investigation(self.case, seen, None, "核对损失", [], questions, knowledge=knowledge, **args)
        self.assertEqual(result.completion_status, "evidence_insufficient")
        self.assertEqual(draft.items["facts"].status, "blocked")

    def test_large_directory_is_deterministic_bounded_and_not_obtained_evidence(self):
        case = self.case.model_copy(deep=True)
        unit = case.info_units[0]
        case.info_units = [unit.model_copy(update={"id": f"node-{i:04d}", "title": "长标题" * 80}) for i in range(200)]
        directory = case_info_directory(case)
        case.info_units.reverse()
        self.assertEqual(directory, case_info_directory(case))
        self.assertEqual(directory["total_count"], 200)
        self.assertEqual(directory["omitted_count"], 200 - len(directory["items"]))
        self.assertTrue(directory["context_truncated"])
        initial = {"goal": "核对", "case_info_directory": directory, "selected_info_units": [], "evidence_context": {"items": []}}
        knowledge = KnowledgeState(initial)
        with patch("app.investigation_state.MAX_CONTEXT_CHARS", 2500):
            view = model_context(initial, knowledge, QuestionState(), {}, None)
        self.assertLessEqual(len(json.dumps(view, ensure_ascii=False)), 2500)
        self.assertEqual(view["case_info_directory"]["omitted_count"], 200 - len(view["case_info_directory"]["items"]))
        self.assertEqual(knowledge.info_units, {})
        self.assertEqual(knowledge.source_refs, {})

    def test_audit_and_generation_record_actual_policy_and_acquisition(self):
        backend = tracing.FakeBackend()
        settings = observability.ObservabilitySettings(True, True, "pk-test", "sk-test", "https://test", "test")
        with patch.object(observability, "load_settings", return_value=settings), patch.object(observability, "_get_backend", return_value=backend):
            self.run_script("核对损失", [f.plan(), f.tool("search_info_units", {"query": "损失", "step_id": "S1"}, "read"), self.insufficient(completed=True)])
        audit = self.audits()[0]
        generations = [s for s in backend.client.spans if s.start.get("as_type") == "generation"]
        for call, span in zip(audit["model_calls"], generations):
            self.assertEqual(call["tool_policy"], span.start["metadata"]["tool_policy"])
            self.assertEqual(call["information_acquisition"], span.start["metadata"]["information_acquisition"])
        read = audit["tool_calls"][1]
        self.assertEqual(read["tool_policy"], "initial_read")
        self.assertEqual(read["information_acquisition_after"]["status"], "read_obtained")
        self.assertEqual(audit["information_acquisition"]["read_attempt_count"], 1)


if __name__ == "__main__":
    unittest.main()
