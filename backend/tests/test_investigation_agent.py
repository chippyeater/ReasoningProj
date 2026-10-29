"""Case A offline loop tests; actual model transport and tracing are mocked."""
import asyncio
from copy import deepcopy
import json
import os
import unittest
from unittest.mock import patch

import test_investigation_tools as fixtures
from app import investigation_agent as agent, investigation_tools as tools, llm_client, observability
from app.errors import AppError

CONFIG = llm_client.LLMConfig("azure", "https://test.openai.azure.com/openai/v1", "mock-model", "test-secret")


def tool(name, args, call_id="call-1"):
    message = {"role": "assistant", "content": None, "tool_calls": [{
        "id": call_id, "type": "function", "function": {
            "name": name, "arguments": json.dumps(args, ensure_ascii=False) if not isinstance(args, str) else args}}]}
    return llm_client.ToolReply("", {"choices": [{"finish_reason": "tool_calls", "message": message}],
                                     "model": "mock-model", "usage": {"total_tokens": 25}}, message)


def final(text):
    message = {"role": "assistant", "content": text}
    return llm_client.ToolReply(text, {"model": "mock-model", "choices": [{"finish_reason": "stop", "message": message}]}, message)


def finish(goal, units=(), relations=(), refs=(), *, status=None, statement=None,
           questions=(), call_id="finish", answer=None, basis="case_material"):
    status = status or ("completed" if units or relations or refs else "evidence_insufficient")
    text = statement or "材料中存在相关陈述，其真实性仍需核实。"
    finding = {"statement": text, "info_unit_ids": list(units), "relation_ids": list(relations),
        "evidence_refs": [{"info_unit_id": uid, "source_ref_index": index} for uid,index in refs]}
    return tool("finish_investigation", {"answer": answer or (text if units else "当前条件不足，仍需核实。"),
        "answer_basis": "general" if status == "unsupported" else basis,
        "findings": [finding] if units or relations or refs else [],
        "open_questions": list(questions) or ([] if status == "completed" else ["仍缺独立材料核实。"]),
        "completion_status": status}, call_id)


class TestInvestigationAgent(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.TestInvestigationTools()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.case, self.data_dir = self.fixture.case, self.fixture.data_dir
        for override in (patch.dict(os.environ, {"LOGICWEAVER_OBSERVABILITY_ENABLED": "false"}),
                         patch.object(llm_client, "load_config", return_value=CONFIG)):
            override.start()
            self.addCleanup(override.stop)

    def tearDown(self):
        self.assertEqual(self.case.model_dump_json(), self.fixture.before_case)
        current = self.fixture.disk_snapshot()
        for name, raw in self.fixture.before_files.items():
            self.assertEqual(current[name], raw)
        self.assertTrue(all("/runs/" in name.replace(chr(92), "/") for name in current.keys() - self.fixture.before_files.keys()))

    def run_script(self, goal, replies, selected=None, case=None, **kwargs):
        calls = []
        async def respond(config, messages, definitions, schema=None, *, tool_choice="auto"):
            calls.append({"messages": deepcopy(messages), "tool_choice": tool_choice, "definitions": definitions, "schema": schema})
            if not callable(replies) and len(calls) > len(replies):
                last_result = json.loads(messages[-1]["content"]) if messages[-1]["role"] == "tool" else {}
                self.fail("Unexpected model continuation: " + json.dumps(last_result.get("error"), ensure_ascii=False))
            reply = replies(calls[-1], len(calls)) if callable(replies) else replies[len(calls) - 1]
            if isinstance(reply, Exception):
                raise reply
            return reply
        with patch.object(llm_client, "call_tools", side_effect=respond):
            result = asyncio.run(agent.run_investigation(case or self.case, goal, selected, **kwargs))
        if not callable(replies):
            self.assertEqual(len(calls), len(replies))
        return result, calls

    def audits(self):
        return [json.loads(path.read_text(encoding="utf-8")) for path in (self.data_dir / self.case.case_id / "runs").glob("*.output.json")]

    def test_fact_query_reads_without_plan_and_finishes_in_three_calls(self):
        answer = "据张某陈述，新租客3月8日入住，尚未独立核实。"
        result, calls = self.run_script("什么时候入住", [tool("search_info_units", {"query": "入住"}),
            tool("get_evidence", {"info_unit_ids": ["U026"]}, "source"),
            finish("什么时候入住", ["U026"], refs=[("U026", 0)], statement=answer)])
        self.assertEqual(result["answer"], answer)
        self.assertEqual(result["model_call_count"], 3)
        self.assertEqual(result["completion_status"], "completed")
        self.assertEqual(result["proposals"], [])
        self.assertNotIn("question_state", result)
        self.assertNotIn("phase", result)
        first = json.loads(calls[0]["messages"][1]["content"])
        self.assertEqual(len(first["case_info_directory"]["items"]), 17)
        self.assertEqual(first["materials"]["info_units"]["items"], [])
        for call in calls:
            self.assertEqual(len(call["messages"]), 2)
            self.assertEqual(len(call["definitions"]), 8)
            self.assertEqual(call["tool_choice"], "auto")
            schema = json.dumps(call["definitions"])
            for field in ("question_ids", "step_id", "update_questions", "update_investigation_steps"):
                self.assertNotIn(field, schema)
            state = json.loads(call["messages"][1]["content"])
            self.assertFalse({"activities", "phase", "remaining_budget", "question_state", "tool_policy"} & state.keys())

    def test_selected_material_and_sources_are_usable_on_first_turn(self):
        result, calls = self.run_script("核对陈述", [finish("核对陈述", ["U029"], refs=[("U029", 0)])], selected=["U029"])
        state = json.loads(calls[0]["messages"][1]["content"])
        self.assertEqual(state["materials"]["info_units"]["items"][0]["id"], "U029")
        self.assertTrue(state["materials"]["source_refs"]["items"][0]["verified_against_cached_text"])
        self.assertEqual(result["tool_budget_used"]["read"], 0)

    def test_capabilities_and_metadata_complete_without_artifacts_or_reads(self):
        for goal, answer, basis in (("你能做什么", "我可以检索案件、核对来源并提出待确认建议。", "general"),
                                   ("有多少信息节点", "当前案件有17个InfoUnit。", "case_metadata")):
            with self.subTest(goal=goal):
                result, calls = self.run_script(goal, [finish(goal, status="completed", answer=answer, basis=basis)])
                self.assertEqual(result["answer"], answer)
                self.assertEqual(result["findings"], [])
                self.assertEqual(result["proposals"], [])
                self.assertEqual(len(calls), 1)

    def test_directory_cannot_establish_case_facts_or_citations(self):
        result, calls = self.run_script("核对损失", [finish("核对损失", call_id="premature"),
            tool("get_evidence", {"info_unit_ids": ["U029"]}, "read"),
            finish("核对损失", ["U029"], refs=[("U029", 0)])])
        self.assertEqual(json.loads(calls[1]["messages"][1]["content"])["recovery"]["code"], "initial_information_required")
        self.assertEqual(result["completion_status"], "completed")

    def test_work_memory_retains_earlier_material_without_duplicate_messages(self):
        result, calls = self.run_script("比较双方陈述", [tool("get_relations", {"info_unit_ids": ["U029"], "relation_types": ["contradict"]}),
            tool("get_evidence", {"info_unit_ids": ["U029", "U036"]}, "sources"),
            tool("search_info_units", {"query": "月租"}, "rent"),
            finish("比较双方陈述", ["U029", "U036"], ["R010"], [("U029", 0), ("U036", 0)])])
        materials = json.loads(calls[-1]["messages"][1]["content"])["materials"]
        obtained = {r["id"] for r in materials["info_units"]["items"]} | {r["info_unit_id"] for r in materials["source_refs"]["items"]}
        self.assertTrue({"U029", "U036", "U004"} <= obtained)
        self.assertTrue({"U029", "U036"} <= {r["info_unit_id"] for r in materials["source_refs"]["items"]})
        self.assertEqual(result["relevant_relation_ids"], ["R010"])
        self.assertTrue(all(len(c["messages"]) == 2 for c in calls))

    def test_empty_and_truncated_reads_keep_distinct_scopes(self):
        result, calls = self.run_script("查找凭证", [tool("search_info_units", {"query": "押金", "limit": 1}),
            tool("search_info_units", {"query": "不存在的凭证"}, "empty"), finish("查找凭证")])
        state = json.loads(calls[-1]["messages"][1]["content"])
        self.assertEqual([n["status"] for n in state["notices"]], ["truncated", "empty"])
        self.assertTrue(state["materials"]["info_units"]["items"])
        self.assertEqual(result["completion_status"], "evidence_insufficient")

    def test_invalid_reference_is_rejected_then_exact_source_can_recover(self):
        _, calls = self.run_script("核对", [tool("search_info_units", {"query": "入住"}),
            finish("核对", ["U026"], refs=[("U026", 99)], call_id="invalid"),
            tool("get_evidence", {"info_unit_ids": ["U026"]}, "source"),
            finish("核对", ["U026"], refs=[("U026", 0)])])
        recovery = json.loads(calls[2]["messages"][1]["content"])["recovery"]
        self.assertEqual(recovery["code"], "invalid_investigation_reference")
        self.assertEqual(recovery["tool"], "finish_investigation")
        self.assertEqual(recovery["arguments"]["findings"][0]["evidence_refs"][0]["source_ref_index"], 99)

    def test_malformed_arguments_and_removed_parameters_never_dispatch(self):
        result, calls = self.run_script("核对", [tool("search_info_units", {"query": "入住", "step_id": "S1"}),
            tool("get_evidence", '{"info_unit_ids":["U029"],"info_unit_ids":["U036"]}', "duplicate-key"),
            tool("get_evidence", {"info_unit_ids": ["U029"]}, "read"),
            finish("核对", ["U029"], refs=[("U029", 0)])])
        self.assertTrue(all(c["result"]["status"] == "invalid_request" for c in self.audits()[0]["tool_calls"][:2]))
        self.assertEqual(result["completion_status"], "completed")

    def test_real_tool_failure_can_end_with_specific_limitation(self):
        with patch.object(tools, "get_evidence", side_effect=OSError("unavailable")):
            result, calls = self.run_script("核对来源", [tool("get_evidence", {"info_unit_ids": ["U029"]}),
                finish("核对来源", questions=["来源文件读取失败，尚不能核对陈述。"] )])
        self.assertEqual(json.loads(calls[-1]["messages"][1]["content"])["recovery"]["code"], "investigation_tool_failed")
        self.assertEqual(result["completion_status"], "evidence_insufficient")
        self.assertEqual(result["evidence_refs"], [])

    def test_unknown_id_failure_cannot_substitute_for_actual_read(self):
        _, calls = self.run_script("核对", [tool("get_evidence", {"info_unit_ids": ["foreign"]}),
            finish("核对", call_id="too-soon"), tool("search_info_units", {"query": "没有这种凭证"}, "empty"), finish("核对")])
        self.assertEqual(json.loads(calls[2]["messages"][1]["content"])["recovery"]["code"], "initial_information_required")

    def test_budget_exhaustion_leaves_proposal_and_finish_available(self):
        reads = [tool("search_info_units", {"query": f"不存在的凭证{i}"}, str(i)) for i in range(8)]
        result, calls = self.run_script("核对", [*reads, finish("核对", status="budget_exhausted")])
        self.assertEqual(result["completion_status"], "budget_exhausted")
        self.assertEqual({d["function"]["name"] for d in calls[-1]["definitions"]}, {
            "finish_investigation", "propose_hypothesis", "propose_evidence_gap", "propose_evidence_link", "propose_hypothesis_status"})

    def test_repeated_read_stops_without_publishing_an_answer(self):
        result, _ = self.run_script("核对", [tool("search_info_units", {"query": "损失"}, str(i)) for i in range(4)])
        self.assertEqual(result["stop_reason"], "no_progress_limit")
        self.assertEqual(result["summary_status"], "failed")
        self.assertIsNone(result["answer"])
        self.assertEqual(sum(t["executed"] for t in self.audits()[0]["tool_calls"]), 1)

    def test_plain_prose_requires_delivery_but_does_not_become_accepted_answer(self):
        result, calls = self.run_script("介绍能力", [final("尚未提交的回答"), finish("介绍能力", status="completed", basis="general", answer="可检索并核对案件材料。")])
        self.assertEqual(calls[1]["tool_choice"], "required")
        self.assertEqual(result["answer"], "可检索并核对案件材料。")

    def test_empty_case_has_no_forced_retrieval(self):
        case = self.case.model_copy(deep=True); case.info_units = []; case.relations = []
        result, _ = self.run_script("调查事实", [finish("调查事实")], case=case)
        self.assertEqual(result["completion_status"], "evidence_insufficient")
        self.assertEqual(result["tool_budget_used"]["read"], 0)

    def test_model_failure_has_no_retry_and_keeps_audit(self):
        with self.assertRaises(llm_client.LLMFailure):
            self.run_script("调查", [llm_client.LLMFailure("llm_timeout", "mock timeout")])
        self.assertEqual(len(self.audits()[0]["model_calls"]), 1)

    def test_unwritable_audit_prevents_model_call(self):
        with patch.object(agent, "write_json", side_effect=OSError("read-only")), patch.object(llm_client, "call_tools") as model:
            with self.assertRaises(AppError):
                asyncio.run(agent.run_investigation(self.case, "调查"))
            model.assert_not_called()

    def test_invalid_initial_inputs_never_call_model(self):
        for goal, ids in (("", None), (" "*10, None), ("字"*2001, None), ("调查", ["foreign"]), ("调查", ["U029", "U029"])):
            with patch.object(llm_client, "call_tools") as model, self.assertRaises(AppError):
                asyncio.run(agent.run_investigation(self.case, goal, ids))
            model.assert_not_called()

    def test_retrieved_fact_can_be_answered_without_forced_findings(self):
        result, _ = self.run_script("合同记载多少月租", [tool("search_info_units", {"query": "月租"}),
            finish("合同记载多少月租", status="completed", answer="节点摘要记载月租3000元，原始合同尚未核对。")])
        self.assertEqual(result["findings"], [])
        self.assertEqual(result["proposals"], [])
        self.assertEqual(result["completion_status"], "completed")

    def test_initial_source_failure_is_visible_and_does_not_grant_source(self):
        case = self.case.model_copy(deep=True)
        next(u for u in case.info_units if u.id == "U029").source_refs[0].quote += "错误转写"
        result, calls = self.run_script("核对陈述", [finish("核对陈述", ["U029"], refs=[("U029", 0)], call_id="bad"),
            finish("核对陈述", status="evidence_insufficient", questions=["初始来源未通过核验，尚不能核对原句。"] )], selected=["U029"], case=case)
        state = json.loads(calls[0]["messages"][1]["content"])
        self.assertEqual(state["materials"]["source_refs"]["items"], [])
        self.assertTrue(state["notices"][0]["errors"])
        self.assertEqual(result["evidence_refs"], [])

    def test_relation_basis_ids_do_not_grant_unretrieved_unit_permission(self):
        case = self.case.model_copy(deep=True)
        next(r for r in case.relations if r.id == "R010").evidence_unit_ids.append("U010")
        _, calls = self.run_script("比较依据", [tool("get_relations", {"info_unit_ids": ["U029"], "relation_types": ["contradict"]}),
            finish("比较依据", ["U010"], ["R010"], call_id="bad"),
            finish("比较依据", ["U029", "U036"], ["R010"])], case=case)
        self.assertEqual(json.loads(calls[2]["messages"][1]["content"])["recovery"]["code"], "invalid_investigation_reference")
