"""Verified initial excerpts and exploratory contracts, without a model service."""
import json
import unittest

import test_investigation_agent as fixture
from app import investigation_agent as agent, investigation_tools as tools
from app.errors import AppError
from app.reasoning_models import ReasoningProposal
from test_reasoning_proposals import suggestion, proposal_call


class TestInitialEvidence(unittest.TestCase):
    setUp = fixture.TestInvestigationAgent.setUp
    tearDown = fixture.TestInvestigationAgent.tearDown
    run_script = fixture.TestInvestigationAgent.run_script
    audits = fixture.TestInvestigationAgent.audits

    def test_initial_sources_allow_findings_and_proposals_without_read_calls(self):
        answer = "张某认为没有损失，仍需核查实际收租情况。"
        result, calls = self.run_script("核对损失并提出假设", [proposal_call(suggestion("hypothesis"), [], "hyp"),
            fixture.present("核对损失并提出假设", ["U029"], refs=[("U029", 0)], statement=answer), fixture.final(answer)],
            selected=["U029"], work_context={"reasoning": {"hypotheses": [], "gaps": []}}, issue_key=self.case.issue_set[1])
        self.assertEqual(result["tool_budget_used"]["read"], 0)
        self.assertEqual(result["evidence_refs"], [{"info_unit_id": "U029", "source_ref_index": 0}])
        initial = json.loads(calls[0]["messages"][1]["content"])
        ref = initial["evidence_context"]["items"][0]
        original = next(u for u in self.case.info_units if u.id == "U029").source_refs[0]
        self.assertEqual(ref["quote_excerpt"], original.quote[:160])
        self.assertEqual((ref["file_id"], ref["start"], ref["end"]), (original.file_id, original.start, original.end))
        self.assertTrue(ref["verified_against_cached_text"])
        self.assertNotIn("quote", ref)

    def test_canvas_sources_are_usable_and_share_the_selected_source_budget(self):
        context = {"canvas": {"info_units": [tools._summary(u) for u in self.case.info_units]}}
        _, calls = self.run_script("核对来源", [fixture.present("核对来源", ["U026"], refs=[("U026", 0)]),
            fixture.final("据笔录陈述，相关日期仍需独立核实。")], selected=["U026"], work_context=context)
        sources = json.loads(calls[0]["messages"][1]["content"])["evidence_context"]
        self.assertEqual(sources["items"][0]["info_unit_id"], "U026")
        self.assertLessEqual(len(sources["items"]), 8)
        self.assertLessEqual(sum(len(r["quote_excerpt"]) for r in sources["items"]), 1280)
        self.assertTrue(sources["has_more"])
        self.assertEqual(len({(r["info_unit_id"], r["source_ref_index"]) for r in sources["items"]}), len(sources["items"]))

    def test_invalid_cached_source_is_reported_and_never_grants_citation(self):
        case = self.case.model_copy(deep=True)
        next(u for u in case.info_units if u.id == "U029").source_refs[0].quote = "不是缓存中的原文"
        result, calls = self.run_script("核对", [fixture.present("核对", ["U029"], refs=[("U029", 0)]),
            fixture.final("该来源无法核验，暂不能判断。"), fixture.final("该来源无法核验，需补充其他材料。")], selected=["U029"], case=case)
        sources = json.loads(calls[0]["messages"][1]["content"])["evidence_context"]
        self.assertEqual(sources["items"], [])
        self.assertEqual(sources["errors"][0]["code"], "source_quote_mismatch")
        self.assertEqual(result["findings"], [])
        self.assertEqual(self.audits()[0]["tool_calls"][0]["result"]["status"], "invalid_request")

    def test_exploratory_hypothesis_and_gap_can_have_no_direct_materials(self):
        h = {**suggestion("hypothesis"), "info_unit_ids": [], "evidence_refs": [], "rationale": "候选解释尚无直接材料支持，需检验。"}
        gap = {**suggestion("evidence_gap"), "info_unit_ids": [], "evidence_refs": [],
               "hypothesis_proposal_indices": [0], "content": "计租起点是否连续？"}
        def decide(call, turn):
            if turn == 1:
                return proposal_call(h, [], "h")
            if turn == 2:
                parent = json.loads(call["messages"][-1]["content"])["proposal_id"]
                return proposal_call({**gap, "hypothesis_proposal_ids": [parent]}, [], "gap")
            return fixture.final("已提出待检验解释与计租缺口，尚无直接材料支持，可采纳后补充核实。")
        result, _ = self.run_script("探索损失解释", decide,
            work_context={"reasoning": {"hypotheses": [], "gaps": []}}, issue_key=self.case.issue_set[1])
        self.assertEqual(len(result["proposals"]), 2)
        self.assertEqual(result["evidence_refs"], [])
        self.assertTrue(all(c["result"]["evidence_status"] == "not_directly_evidenced" for c in self.audits()[0]["tool_calls"]))
        definitions = {d["function"]["name"]: d["function"]["parameters"] for d in agent.tool_definitions()}
        for name in ("propose_hypothesis", "propose_evidence_gap"):
            self.assertEqual(definitions[name]["properties"]["evidence_refs"].get("minItems", 0), 0)
        for name in ("propose_evidence_link", "propose_hypothesis_status"):
            self.assertEqual(definitions[name]["properties"]["evidence_refs"]["minItems"], 1)
            with self.subTest(name=name), self.assertRaises(ValueError):
                ReasoningProposal.model_validate({**suggestion(name.removeprefix("propose_"), "hyp-existing"), "evidence_refs": []})

    def test_initial_sources_do_not_authorize_unknown_out_of_range_or_unprovided_citations(self):
        seen = {"units": {"U029"}, "relations": set(), "evidence": {("U029", 0)}}
        for uid, index in (("foreign-case-unit", 0), ("U029", 999), ("U026", 0)):
            with self.subTest(uid=uid, index=index), self.assertRaises(AppError):
                from app.investigation_actions import artifacts
                artifacts(self.case, seen, None, "核对", [], [{"statement": "陈述", "info_unit_ids": [uid],
                    "relation_ids": [], "evidence_refs": [{"info_unit_id": uid, "source_ref_index": index}]}], [], "evidence_insufficient")


if __name__ == "__main__":
    unittest.main()
