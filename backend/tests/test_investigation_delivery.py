"""Autonomous Proposal delivery and matching local/Langfuse observations."""
import json
import unittest
from unittest.mock import patch

import test_investigation_agent as f
import test_observability as tracing
from app import investigation_agent as agent, observability
from app.prompt_loader import load_prompt


class TestInvestigationDelivery(unittest.TestCase):
    setUp = f.TestInvestigationAgent.setUp
    tearDown = f.TestInvestigationAgent.tearDown
    run_script = f.TestInvestigationAgent.run_script
    audits = f.TestInvestigationAgent.audits

    def context(self):
        return {"current_issue": self.case.issue_set[1], "reasoning": {"hypotheses": [], "gaps": [], "links": [], "has_more": False}}

    def test_two_distinct_hypotheses_and_shared_gap_use_real_returned_dependencies(self):
        goal = "读取双方材料，提出两个竞争性假设和一个共同证据缺口"
        def respond(call, turn):
            state = json.loads(call["messages"][1]["content"])
            self.assertEqual(state["goal"], goal)
            proposals = state["proposals"]
            if turn == 1:
                return f.tool("get_evidence", {"info_unit_ids": ["U029", "U036"]}, "read")
            if len(proposals) < 2:
                uid = "U029" if not proposals else "U036"
                return f.tool("propose_hypothesis", {
                    "content": "租金接续支付，入住间隔未造成少收租金" if not proposals else "新租约延后计租，空置期间确有租金缺口",
                    "rationale": "待检验解释，需要对照计租与实收记录，单方陈述不能证实。",
                    "info_unit_ids": [uid], "evidence_refs": [{"info_unit_id": uid, "source_ref_index": 0}]}, f"hyp-{len(proposals)}")
            if len(proposals) == 2:
                return f.tool("propose_evidence_gap", {"content": "新旧租约的计租期间与实际收款能否衔接？",
                    "hypothesis_ids": [], "hypothesis_proposal_ids": [p["proposal_id"] for p in proposals],
                    "rationale": "收款与计租记录可以区分是否实际少收租金。", "info_unit_ids": [], "evidence_refs": []}, "gap")
            self.assertEqual(len(state["materials"]["source_refs"]["items"]), 3)
            return f.finish(goal, status="completed", answer="已提出两个待检验解释及一个共同缺口，请逐项审阅采纳。")
        result, calls = self.run_script(goal, respond, work_context=self.context(), issue_key=self.case.issue_set[1])
        proposals = result["proposals"]
        self.assertEqual([p["kind"] for p in proposals], ["hypothesis", "hypothesis", "evidence_gap"])
        self.assertNotEqual(proposals[0]["content"], proposals[1]["content"])
        self.assertEqual(proposals[2]["hypothesis_proposal_ids"], [p["proposal_id"] for p in proposals[:2]])
        self.assertEqual(result["findings"], [])
        self.assertEqual(len(calls), 5)
        self.assertIn("proposal_receipt", self.audits()[0])

    def test_invalid_proposal_does_not_appear_in_memory_or_receipt(self):
        result, calls = self.run_script("提出解释", [f.tool("propose_hypothesis", {
            "content": "未验证的解释", "rationale": "没有读取来源", "info_unit_ids": ["U029"],
            "evidence_refs": [{"info_unit_id": "U029", "source_ref_index": 0}]}),
            f.finish("提出解释", status="unsupported", questions=["当前引用尚未取得"], basis="general")],
            work_context=self.context(), issue_key=self.case.issue_set[1])
        self.assertEqual(json.loads(calls[1]["messages"][1]["content"])["proposals"], [])
        self.assertEqual(result["proposals"], [])
        self.assertNotIn("proposal_receipt", self.audits()[0])

    def test_generations_and_audits_track_exact_single_prompt_and_request(self):
        backend = tracing.FakeBackend()
        with patch.object(observability, "load_settings", return_value=tracing.SETTINGS), patch.object(observability, "_get_backend", return_value=backend):
            result, calls = self.run_script("核对陈述", [f.tool("get_evidence", {"info_unit_ids": ["U029"]}),
                f.finish("核对陈述", ["U029"], refs=[("U029", 0)])])
        audit = self.audits()[0]
        prompt = load_prompt(agent.PROMPT_PATH, agent.PROMPT_NAME)
        generations = [s for s in backend.client.spans if s.start.get("as_type") == "generation"]
        self.assertEqual(len(generations), 2)
        for recorded, span, call in zip(audit["model_calls"], generations, calls):
            self.assertEqual(recorded["request"], span.start["input"])
            self.assertEqual(call["messages"][0]["content"], prompt.content)
            for key, value in prompt.metadata(f.CONFIG.model).items():
                self.assertEqual(recorded[key], value)
                self.assertEqual(span.start["metadata"][key], value)
            self.assertNotIn("phase", recorded)
        self.assertEqual(audit["information_acquisition"]["obtained_source_count"], 2)
