"""Missing Finding citations expose obtained pointers, never guessed sources."""
from copy import deepcopy
import json
import unittest

import test_investigation_agent as f
from app import investigation_agent as agent, investigation_tools as tools
from app.errors import AppError


class TestInvestigationFindingSources(unittest.TestCase):
    setUp = f.TestInvestigationAgent.setUp
    tearDown = f.TestInvestigationAgent.tearDown
    run_script = f.TestInvestigationAgent.run_script
    audits = f.TestInvestigationAgent.audits

    def check_sources(self, state, case):
        recovery = state["recovery"]
        self.assertEqual(recovery["code"], "finding_sources_missing")
        obtained = {(row["info_unit_id"], row["source_ref_index"]) for row in state["materials"]["source_refs"]["items"]}
        for error in recovery["validation_details"]["errors"]:
            uid = error["info_unit_id"]
            self.assertEqual(error["info_unit_title"], next(u.title for u in case.info_units if u.id == uid))
            self.assertEqual(error["loc"], ["findings", error["finding_index"], "evidence_refs"])
            self.assertEqual(error["obtained_source_ref_indices"], sorted(index for key, index in obtained if key == uid))
            self.assertEqual(error["available_evidence_refs"], [
                {"info_unit_id": uid, "source_ref_index": index} for index in error["obtained_source_ref_indices"]])
            self.assertTrue(error["available_evidence_refs"])
        for step in recovery["next_action"]["steps"]:
            self.assertEqual(step["tool"], "finish_investigation")
            self.assertTrue(step["evidence_refs"])
            self.assertIn("already obtained", step["instruction"])
            self.assertIn("open_questions", step["instruction"])
        return recovery

    def test_open_phase_reuses_existing_sources_without_granting_unread_sources(self):
        missing_title = next(u.title for u in self.case.info_units if u.id == "U036")
        def respond(call, turn):
            if turn == 1:
                return f.tool("search_info_units", {"query": missing_title}, "summary")
            if turn == 2:
                return f.tool("get_evidence", {"info_unit_ids": ["U029"]}, "sources")
            if turn == 3:
                return f.finish("核对", ["U029", "U036"], call_id="missing-sources")
            state = json.loads(call["messages"][1]["content"])
            recovery = self.check_sources(state, self.case)
            self.assertIn("get_evidence", [d["function"]["name"] for d in call["definitions"]])
            self.assertEqual({e["info_unit_id"] for e in recovery["validation_details"]["errors"]}, {"U029"})
            refs = [(ref["info_unit_id"], ref["source_ref_index"]) for step in recovery["next_action"]["steps"] for ref in step["evidence_refs"]]
            return f.finish("核对", ["U029", "U036"], refs=refs)
        result, _ = self.run_script("核对", respond)
        self.assertEqual(result["summary_status"], "completed")
        self.assertEqual(result["tool_budget_used"], {"read": 2, "proposal": 0, "finish": 2})
        self.assertTrue(all(ref["info_unit_id"] == "U029" for ref in result["evidence_refs"]))

    def test_closed_phase_keeps_obtained_citation_repairs_executable(self):
        for reason in ("budget", "no_progress"):
            with self.subTest(reason=reason):
                case = self.case.model_copy(deep=True)
                if reason == "budget":
                    seed = next(u for u in case.info_units if u.id == "U029")
                    case.info_units = [seed.model_copy(deep=True, update={"id": f"budget-{i}", "title": f"预算材料{i}"}) for i in range(8)]
                    case.relations = []
                    uid, selected = "budget-0", None
                    reads = [f.tool("get_evidence", {"info_unit_ids": [f"budget-{i}"]}, f"read-{i}") for i in range(8)]
                else:
                    uid, selected = "U029", ["U029"]
                    reads = [f.tool("get_evidence", {"info_unit_ids": [uid]}, "source")] + [
                        f.tool("search_info_units", {"query": f"不存在凭证{i}"}, f"read-{i}") for i in range(3)]
                def respond(call, turn):
                    if turn <= len(reads):
                        return reads[turn - 1]
                    self.assertEqual([d["function"]["name"] for d in call["definitions"]], ["finish_investigation"])
                    if turn == len(reads) + 1:
                        return f.finish("核对", [uid], call_id="missing-sources")
                    state = json.loads(call["messages"][1]["content"])
                    recovery = self.check_sources(state, case)
                    self.assertEqual(recovery["constraints"]["allowed_tools"], ["finish_investigation"])
                    if reason == "no_progress":
                        self.assertEqual(recovery["validation_details"]["errors"][0]["obtained_source_ref_indices"], [0, 1])
                        self.assertGreater(len(next(u for u in case.info_units if u.id == uid).source_refs), 1)
                    refs = [(ref["info_unit_id"], ref["source_ref_index"]) for ref in recovery["next_action"]["steps"][0]["evidence_refs"]]
                    return f.finish("核对", [uid], refs=refs, status="budget_exhausted" if reason == "budget" else "completed")
                result, _ = self.run_script("核对", respond, case=case, selected=selected)
                self.assertEqual(result["summary_status"], "completed")
                self.assertEqual(result["tool_budget_used"]["finish"], 2)

    def test_multiple_findings_report_their_exact_positions_and_materials(self):
        findings = [{"statement": "材料存在相关陈述，仍需独立核验。", "info_unit_ids": [uid], "relation_ids": [],
            "evidence_refs": [{"info_unit_id": uid, "source_ref_index": 0}] if i == 0 else []}
            for i, uid in enumerate(("U029", "U036", "U029"))]
        args = {"answer": "双方陈述仍需核验。", "answer_basis": "case_material", "findings": findings,
                "open_questions": [], "completion_status": "completed"}
        def respond(call, turn):
            if turn == 1:
                return f.tool("get_evidence", {"info_unit_ids": ["U029", "U036"]}, "read")
            if turn == 2:
                return f.tool("finish_investigation", args, "missing")
            recovery = self.check_sources(json.loads(call["messages"][1]["content"]), self.case)
            self.assertEqual([(e["finding_index"], e["info_unit_id"]) for e in recovery["validation_details"]["errors"]], [(1, "U036"), (2, "U029")])
            corrected = deepcopy(args)
            for step in recovery["next_action"]["steps"]:
                corrected["findings"][step["finding_index"]]["evidence_refs"] = step["evidence_refs"]
            return f.tool("finish_investigation", corrected, "fixed")
        result, _ = self.run_script("核对", respond)
        self.assertEqual(result["summary_status"], "completed")
        self.assertEqual(len(result["findings"]), 3)

    def test_no_obtained_source_recovery_never_invents_indices(self):
        for closed in (False, True):
            with self.subTest(closed=closed):
                response = tools._failure(AppError("finding_sources_missing", "Finding needs a source"))
                response["error"]["validation_details"] = {"errors": [{"loc": ["findings", 1, "evidence_refs"],
                    "code": "finding_sources_missing", "finding_index": 1, "info_unit_id": "U029",
                    "obtained_source_ref_indices": [], "available_evidence_refs": []}]}
                agent._tool_recovery(response, "finish_investigation", case=self.case)
                if closed:
                    agent._finish_only_recovery(response["error"])
                error = response["error"]["validation_details"]["errors"][0]
                step = response["error"]["next_action"]["steps"][0]
                self.assertEqual(error["obtained_source_ref_indices"], [])
                self.assertEqual(error["available_evidence_refs"], [])
                self.assertFalse(step.get("evidence_refs"))
                self.assertIn("open_questions", step["instruction"])
                self.assertEqual(step["tool"], "finish_investigation" if closed else "get_evidence")
                if not closed:
                    self.assertEqual(step["arguments"], {"info_unit_ids": ["U029"]})
                else:
                    self.assertNotIn("arguments", step)

    def test_unavailable_sources_cannot_be_claimed_as_obtained(self):
        for closed in (False, True):
            with self.subTest(closed=closed):
                case = self.case.model_copy(deep=True)
                for ref in next(u for u in case.info_units if u.id == "U029").source_refs:
                    ref.quote += "未通过缓存核验的转写"
                reads = [f.tool("search_info_units", {"query": f"不存在凭证{i}"}, f"read-{i}") for i in range(3)] if closed else []
                result, calls = self.run_script("核对", [*reads,
                    f.finish("核对", ["U029"], refs=[("U029", 0)], call_id="unobtained"),
                    f.finish("核对", questions=["来源未通过核验，不能确认结论。" ])], case=case, selected=["U029"])
                state = json.loads(calls[-1]["messages"][1]["content"])
                self.assertEqual(state["materials"]["source_refs"]["items"], [])
                self.assertEqual(result["evidence_refs"], [])
                self.assertEqual(result["findings"], [])
                self.assertTrue(all(not step.get("evidence_refs") for step in state["recovery"]["next_action"]["steps"]))
                if closed:
                    self.assertTrue(all(step["tool"] == "finish_investigation" for step in state["recovery"]["next_action"]["steps"]))
