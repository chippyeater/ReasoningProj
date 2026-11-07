"""Single-node evidence pages through native Agent dispatch, memory and proposals."""
import json
import unittest

import test_investigation_agent as f
from app import investigation_agent as agent, investigation_state as state, investigation_tools as tools
from app.errors import AppError
from app.issue_workspaces import get_issue_workspaces


class TestInvestigationEvidence(unittest.TestCase):
    setUp = f.TestInvestigationAgent.setUp
    tearDown = f.TestInvestigationAgent.tearDown
    run_script = f.TestInvestigationAgent.run_script
    audits = f.TestInvestigationAgent.audits

    def test_schema_defaults_and_rejection_of_the_removed_batch_parameter(self):
        definition = next(d["function"] for d in agent.tool_definitions() if d["function"]["name"] == "get_evidence")
        self.assertEqual(set(definition["parameters"]["properties"]), {"info_unit_id", "offset", "limit"})
        self.assertEqual(definition["parameters"]["properties"]["limit"]["maximum"], 12)
        self.assertFalse(definition["parameters"]["additionalProperties"])
        self.assertEqual(agent._arguments("get_evidence", '{"info_unit_id":"U029"}'),
                         {"info_unit_id": "U029", "offset": 0, "limit": 12})
        self.assertEqual(agent._fingerprint("get_evidence", agent._arguments("get_evidence", '{"info_unit_id":"U029"}')),
                         agent._fingerprint("get_evidence", agent._arguments("get_evidence", '{"info_unit_id":"U029","offset":0,"limit":12}')))
        for args in ({"info_unit_ids": ["U029"]}, {"info_unit_id": "U029", "info_unit_ids": ["U029"]}):
            with self.assertRaises(AppError) as caught:
                agent._arguments("get_evidence", json.dumps(args))
            self.assertTrue(any(error["code"] == "extra_forbidden" for error in caught.exception.details["errors"]))

    def test_pages_accumulate_and_can_ground_proposals_and_atomic_commit(self):
        case = self.case.model_copy(deep=True)
        unit = next(u for u in case.info_units if u.id == "U029")
        unit.source_refs = [unit.source_refs[0].model_copy(deep=True) for _ in range(25)]
        def respond(call, turn):
            view = json.loads(call["messages"][1]["content"])
            if turn <= 3:
                offset = 0 if turn == 1 else next(n["next_offset"] for n in view["notices"] if n["tool"] == "get_evidence")
                self.assertEqual(offset, (turn - 1) * 12)
                return f.tool("get_evidence", {"info_unit_id": "U029", "offset": offset, "limit": 12}, f"page-{offset}")
            self.assertFalse(any(n["tool"] == "get_evidence" and n["has_more"] for n in view["notices"]))
            if turn == 4:
                self.assertEqual(view["materials"]["source_refs"]["items"][0]["source_ref_index"], 24)
                return f.tool("propose_hypothesis", {"content": "实际收款可能连续", "rationale": "仅为待核实命题",
                    "info_unit_ids": ["U029"]}, "hypothesis")
            if turn == 5:
                return f.tool("propose_evidence_link", {"hypothesis_id": None,
                    "hypothesis_proposal_ids": [view["proposals"][0]["proposal_id"]],
                    "info_unit_id": "U029", "meaning": "related", "rationale": "该来源为当事人陈述"}, "link")
            return f.finish("核对来源", status="completed")
        result, _ = self.run_script("核对来源", respond, case=case, issue_key=case.issue_set[1])
        self.assertEqual(result["workspace_commit"]["status"], "applied", result)
        self.assertEqual(result["tool_budget_used"], {"read": 3, "proposal": 2, "finish": 1})
        self.assertEqual(len(self.audits()[0]["knowledge_state"]["source_refs"]), 25)
        self.assertEqual([r["source_ref_index"] for r in result["evidence_refs"]], list(range(25)))
        reasoning = get_issue_workspaces(case).workspaces[case.issue_set[1]].reasoning
        self.assertEqual(len(reasoning.hypotheses), 1)
        self.assertEqual(len(reasoning.links), 1)
        self.assertEqual(len(reasoning.investigation_runs[result["run_id"]].evidence), 25)
        self.assertTrue(all("evidence_refs" not in p for p in result["proposals"]))

    def test_only_returned_pages_enter_the_run_read_record(self):
        from test_reasoning_proposals import suggestion, proposal_call
        proposal = suggestion("hypothesis")
        result, calls = self.run_script("核对", [
            f.tool("get_evidence", {"info_unit_id": "U029", "limit": 1}, "first"),
            proposal_call(proposal, [], "obtained-unit"),
            f.tool("get_evidence", {"info_unit_id": "U029", "offset": 1, "limit": 1}, "second"),
            f.finish("核对", status="completed")], issue_key=self.case.issue_set[1])
        self.assertEqual([ref["source_ref_index"] for ref in json.loads(calls[2]["messages"][1]["content"])["materials"]["source_refs"]["items"]], [0])
        self.assertEqual([ref["source_ref_index"] for ref in result["evidence_refs"]], [0, 1])
        self.assertEqual(len(result["proposals"]), 1)
        self.assertEqual(result["workspace_commit"]["status"], "applied")

    def test_unknown_id_and_bad_page_fields_have_parameter_local_recovery(self):
        for args, field in (({"info_unit_id": "foreign"}, "info_unit_id"),
                            ({"info_unit_id": "U029", "offset": -1}, "offset"),
                            ({"info_unit_id": "U029", "limit": 13}, "limit")):
            with self.subTest(args=args):
                response = tools.get_evidence(self.case, **args)
                agent._tool_recovery(response, "get_evidence", args, self.case)
                self.assertEqual(response["error"]["validation_details"]["errors"][0]["argument_loc"], [field])

    def test_full_8000_character_source_is_visible_and_later_page_can_replace_its_view(self):
        initial = {"goal": "核对原文"}
        memory = state.KnowledgeState(initial)
        def merge(index, quote, has_more):
            args = {"info_unit_id": "U029", "offset": index, "limit": 1}
            response = {"status": "truncated" if has_more else "ok", "error": None, "has_more": has_more,
                "total_count": 2, "next_offset": index + 1 if has_more else None, "items": [
                    {"info_unit_id": "U029", "source_ref_index": index, "source_ref": {"quote": quote}}]}
            memory.merge("get_evidence", args, response)
        merge(0, "甲" * 8000, True)
        self.assertEqual(state.model_context(initial, memory)["materials"]["source_refs"]["items"][0]["source_ref"]["quote"], "甲" * 8000)
        merge(1, "乙" * 8000, False)
        view = state.model_context(initial, memory)
        self.assertEqual(view["materials"]["source_refs"]["items"][0]["source_ref_index"], 1)
        self.assertEqual(view["materials"]["source_refs"]["omitted_ids"], ["U029:0"])
        self.assertEqual(len(memory.source_refs), 2)
        self.assertFalse(view["notices"])
