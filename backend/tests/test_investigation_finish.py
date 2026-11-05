"""Finding-free Finish and server-recorded source provenance, without a real LLM."""
import json
import unittest

import test_investigation_agent as f
from app import investigation_agent as agent
from app.errors import AppError


class TestInvestigationFinish(unittest.TestCase):
    setUp = f.TestInvestigationAgent.setUp
    tearDown = f.TestInvestigationAgent.tearDown
    run_script = f.TestInvestigationAgent.run_script
    audits = f.TestInvestigationAgent.audits

    def test_finish_schema_and_result_model_have_no_finding(self):
        schema = next(d["function"]["parameters"] for d in agent.tool_definitions()
                      if d["function"]["name"] == "finish_investigation")
        self.assertEqual(set(schema["properties"]), {"answer", "answer_basis", "open_questions", "completion_status"})
        self.assertEqual(set(schema["required"]), set(schema["properties"]))
        self.assertNotIn("Finding", agent.InvestigationResult.model_json_schema()["$defs"])
        self.assertNotIn("findings", agent.InvestigationResult.model_fields)
        self.assertNotIn("Finding", schema.get("description", ""))

    def test_obtained_sources_are_retained_without_a_repeated_citation_handoff(self):
        result, calls = self.run_script("核对", [
            f.tool("get_evidence", {"info_unit_id": "U029"}, "sources-a"),
            f.tool("get_evidence", {"info_unit_id": "U036"}, "sources-b"),
            f.finish("核对", status="completed", answer="材料存在双方陈述，真实性仍待独立核实。")])
        obtained = {(row["info_unit_id"], row["source_ref_index"])
                    for row in self.audits()[0]["knowledge_state"]["source_refs"].values()}
        self.assertEqual({(ref["info_unit_id"], ref["source_ref_index"]) for ref in result["evidence_refs"]}, obtained)
        self.assertTrue(obtained)
        self.assertEqual(result["tool_budget_used"]["finish"], 1)
        self.assertEqual(result["summary_status"], "completed")
        self.assertNotIn("findings", result)
        self.assertNotIn("findings", json.loads(calls[-1]["messages"][1]["content"]))
        self.assertEqual(result["proposals"], [])

    def test_summary_reads_and_directory_do_not_grant_source_pointers(self):
        result, _ = self.run_script("核对", [f.tool("search_info_units", {"query": "损失"}),
            f.finish("核对", status="completed", answer="摘要包含损失主张，原文尚未核对。")])
        self.assertTrue(result["relevant_info_unit_ids"])
        self.assertEqual(result["evidence_refs"], [])
        self.assertNotIn("findings", result)

    def test_failed_source_read_cannot_become_a_source_in_the_result(self):
        case = self.case.model_copy(deep=True)
        for ref in next(u for u in case.info_units if u.id == "U029").source_refs:
            ref.quote += "未通过缓存核验的转写"
        result, _ = self.run_script("核对", [f.tool("get_evidence", {"info_unit_id": "U029"}),
            f.finish("核对", questions=["来源未通过核验，不能确认结论。"])], case=case, selected=["U029"])
        self.assertEqual(result["evidence_refs"], [])
        self.assertEqual(result["completion_status"], "evidence_insufficient")

    def test_new_finish_rejects_removed_field_and_can_recover(self):
        old_args = {"answer": "说明能力。", "answer_basis": "general", "findings": [],
                    "open_questions": [], "completion_status": "completed"}
        with self.assertRaises(AppError) as caught:
            agent._arguments("finish_investigation", json.dumps(old_args))
        self.assertEqual(caught.exception.details["errors"][0]["code"], "extra_forbidden")
        result, calls = self.run_script("说明能力", [f.tool("finish_investigation", old_args, "old"),
            f.finish("说明能力", basis="general", status="completed")])
        recovery = json.loads(calls[-1]["messages"][1]["content"])["recovery"]
        self.assertEqual(recovery["next_action"]["steps"][0]["loc"], ["findings"])
        self.assertEqual(result["summary_status"], "completed")
