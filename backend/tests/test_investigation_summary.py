"""Concise summary handoff, rejection and artifact survival, without a real LLM."""
import json
import unittest

import test_investigation_agent as fixture
from test_reasoning_proposals import suggestion, proposal_call
from app import investigation_agent as agent
from app.errors import AppError
from app.reasoning_models import ReasoningProposal


class TestSummaryValidation(unittest.TestCase):
    def proposal(self, **values):
        return ReasoningProposal.model_validate({**suggestion("hypothesis"), **values})

    def test_short_facts_and_modestly_longer_complex_answers_preserve_markdown(self):
        for answer in ("据张某陈述，替租人于 3 月 8 日搬入，尚未独立核实。",
                       "实际损失**尚未证实**。\n\n已提出假设和证据缺口，优先核对计租日与收款记录；可查看、采纳或忽略建议。",
                       "核" * agent.MAX_SUMMARY_CHARACTERS):
            self.assertEqual(agent._validate_summary(" \n" + answer + "\n", []), answer)

    def test_invalid_summaries_have_specific_diagnostics_and_are_not_truncated(self):
        for text, code in ((None, "invalid_summary"), (" \n", "invalid_summary"),
                           ("字" * 241, "summary_too_long"), ('{"answer":"结论"}', "structured_final_reply")):
            with self.subTest(code=code), self.assertRaises(AppError) as raised:
                agent._validate_summary(text, [])
            self.assertEqual(raised.exception.code, "investigation_summary_invalid")
            self.assertEqual(raised.exception.details["category"], "summary")
            self.assertEqual(raised.exception.details["errors"][0]["code"], code)
            self.assertEqual(raised.exception.details["errors"][0]["loc"], ["answer"])

    def test_complete_proposal_content_and_rationale_rejected_even_with_formatting(self):
        content = "房屋交还后新租约尚未开始计租且房东未收到该时段租金因此可能存在实际损失"
        rationale = "入住日期与计租日期可能不同仅凭租客笔录不足以证明租金收入确实发生减少"
        proposal = self.proposal(content=content, rationale=rationale)
        for field, value in (("content", content), ("rationale", rationale)):
            copied = "**" + "，".join(value) + "**"
            with self.subTest(field=field), self.assertRaises(AppError) as raised:
                agent._validate_summary("调查尚待核实。\n\n" + copied, [proposal])
            self.assertEqual(raised.exception.details["errors"][0], {
                "loc": ["answer"], "code": "proposal_repeated", "proposal_index": 0, "field": field})

    def test_shared_short_phrase_is_not_confused_with_copying_a_report(self):
        answer = "实际租金损失尚未证实。已提出假设，建议优先核实计租日，可查看后采纳或忽略。"
        self.assertEqual(agent._validate_summary(answer, [self.proposal(content="实际租金损失尚未证实")]), answer)


class TestSummaryHandoff(unittest.TestCase):
    setUp = fixture.TestInvestigationAgent.setUp
    tearDown = fixture.TestInvestigationAgent.tearDown
    run_script = fixture.TestInvestigationAgent.run_script
    audits = fixture.TestInvestigationAgent.audits

    def test_success_inventory_excludes_failed_calls_and_is_in_actual_request(self):
        hypothesis = suggestion("hypothesis")
        gap = suggestion("evidence_gap")
        answer = "实际租金损失尚未证实。已提出一条假设和一项证据缺口，优先核对计租与收款；可查看后采纳或忽略。"
        def decide(call, turn):
            if turn == 1:
                return fixture.tool("get_evidence", {"info_unit_ids": ["U029"]})
            if turn == 2:
                return proposal_call(hypothesis, [], "hypothesis")
            if turn == 3:
                # Failure is visible to the model, but does not become a pending suggestion.
                return proposal_call({**gap, "hypothesis_proposal_ids": ["proposal-" + "f" * 32]}, [], "bad-gap")
            if turn == 4:
                parent = next(json.loads(m["content"])["proposal_id"] for m in call["messages"] if m["role"] == "tool" and json.loads(m["content"]).get("pending"))
                return proposal_call({**gap, "hypothesis_proposal_ids": [parent]}, [], "gap")
            brief = json.loads(call["messages"][0]["content"].split("本轮界面交接状态（后端生成）：")[-1])
            self.assertEqual(brief["pending_count"], 2)
            self.assertEqual(brief["failed_tool_calls"], 1)
            self.assertEqual([p["kind"] for p in brief["successful_proposals"]], ["hypothesis", "evidence_gap"])
            self.assertTrue(all(p["status"] == "pending_user_confirmation" for p in brief["successful_proposals"]))
            self.assertNotIn("rationale", json.dumps(brief))
            return fixture.final(answer)
        result, calls = self.run_script("房东是否存在实际空置损失", decide,
            work_context={"reasoning": {"hypotheses": [], "gaps": []}}, issue_key=self.case.issue_set[1])
        self.assertEqual(result["answer"], answer)
        self.assertEqual(len(result["proposals"]), 2)
        self.assertEqual(result["summary_status"], "completed")
        audit = self.audits()[0]
        self.assertEqual(audit["model_calls"][-1]["request"]["messages"], calls[-1]["messages"])
        self.assertEqual(result["tool_attempt_count"], 4)

    def test_no_proposals_context_and_fact_answer(self):
        answer = "据张某陈述，替租人于 3 月 8 日搬入，尚需独立核实。"
        result, calls = self.run_script("搬入时间", [
            fixture.tool("get_evidence", {"info_unit_ids": ["U026"]}),
            fixture.present("搬入时间", ["U026"], refs=[("U026", 0)], statement=answer), fixture.final(answer)])
        brief = json.loads(calls[-1]["messages"][0]["content"].split("本轮界面交接状态（后端生成）：")[-1])
        self.assertEqual(brief["successful_proposals"], [])
        self.assertEqual(brief["pending_count"], 0)
        self.assertEqual(result["answer"], answer)
        self.assertEqual(result["evidence_refs"], [{"info_unit_id": "U026", "source_ref_index": 0}])

    def test_rejected_summary_preserves_pending_proposal_and_cited_findings_without_retry(self):
        long_content = "房屋交还后新租约尚未开始计租且房东未收到该时段租金因此可能存在实际损失"
        result, calls = self.run_script("房东是否存在实际空置损失", [
            fixture.tool("get_evidence", {"info_unit_ids": ["U029"]}),
            proposal_call({**suggestion("hypothesis"), "content": long_content}, [], "hyp"),
            fixture.present("房东是否存在实际空置损失", ["U029"], refs=[("U029", 0)]),
            fixture.final("调查结论如下：" + long_content)],
            work_context={"reasoning": {"hypotheses": [], "gaps": []}}, issue_key=self.case.issue_set[1])
        self.assertEqual(len(calls), 4)  # No hidden summary retry or validation LLM.
        self.assertIsNone(result["answer"])
        self.assertEqual(result["summary_status"], "failed")
        self.assertEqual(len(result["proposals"]), 1)
        self.assertEqual(len(result["findings"]), 1)
        self.assertEqual(result["evidence_refs"], [{"info_unit_id": "U029", "source_ref_index": 0}])
        self.assertEqual(self.audits()[0]["status"], "partial")
        self.assertEqual(self.audits()[0]["error"]["validation_details"]["errors"][0]["code"], "proposal_repeated")


if __name__ == "__main__":
    unittest.main()
