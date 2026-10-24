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

    def test_rejected_answer_is_recoverable_finish_error_without_partial_findings(self):
        result, calls = self.run_script("核对", [fixture.finish("核对", ["U029"], refs=[("U029", 0)], answer="字"*241, call_id="bad"),
            fixture.finish("核对", ["U029"], refs=[("U029", 0)], answer="陈述仍需核实。")], selected=["U029"])
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["answer"], "陈述仍需核实。")
        error = json.loads(calls[1]["messages"][-1]["content"])["error"]
        self.assertEqual(error["validation_details"]["errors"][0]["code"], "summary_too_long")
        self.assertEqual(json.loads(calls[1]["messages"][1]["content"])["knowledge_state"]["findings"]["items"], [])

    def test_successful_proposals_survive_model_failure_before_finish(self):
        from app import llm_client
        result, _ = self.run_script("查损失", [proposal_call(suggestion("hypothesis"), [], "h"),
            llm_client.LLMFailure("llm_timeout", "mock timeout")], selected=["U029"],
            work_context={"reasoning": {"hypotheses": [], "gaps": []}}, issue_key=self.case.issue_set[1])
        self.assertEqual(result["summary_status"], "failed")
        self.assertIsNone(result["answer"])
        self.assertEqual(len(result["proposals"]), 1)
        self.assertEqual(result["findings"], [])
        self.assertEqual(self.audits()[0]["status"], "partial")
        self.assertTrue(self.audits()[0]["proposal_receipt"])
