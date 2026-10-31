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

    def test_search_content_supports_two_hypotheses_and_one_shared_condition_without_sources(self):
        goal = "提出两个竞争性损失解释和一个共同证据缺口"
        def respond(call, turn):
            state = json.loads(call["messages"][1]["content"])
            self.assertFalse(state["materials"]["source_refs"]["items"])
            if turn == 1:
                return f.tool("search_info_units", {"query": "损失"}, "read")
            units = {u["id"] for u in state["materials"]["info_units"]["items"]}
            self.assertTrue({"U029", "U036"} <= units)
            proposals = state["proposals"]
            if len(proposals) < 2:
                return f.tool("propose_hypothesis", {
                    "content": "新租客租金承接原计租期间，因此未少收租金" if not proposals else "计租未衔接，存在少收租金的期间",
                    "rationale": "双方损失陈述不同，摘要可支持备选解释，尚未核验原文。",
                    "info_unit_ids": ["U029", "U036"], "evidence_refs": []}, f"hyp-{len(proposals)}")
            if len(proposals) == 2:
                return f.tool("propose_evidence_gap", {"content": "新租客的计租起点是否覆盖原租客搬离当日？",
                    "rationale": "核对新租约中的计租起点，可区分租金承接与计租中断两种解释。",
                    "info_unit_ids": ["U029", "U036"], "evidence_refs": [], "hypothesis_ids": [],
                    "hypothesis_proposal_ids": [p["proposal_id"] for p in proposals]}, "gap")
            return f.finish(goal, status="completed", answer="已提交两种待检验解释及一个共同核查条件，请审阅。")
        result, calls = self.run_script(goal, respond, work_context=self.context(), issue_key=self.case.issue_set[1])
        self.assertEqual(result["completion_status"], "completed")
        self.assertEqual(result["tool_budget_used"]["read"], 1)
        self.assertEqual(len(calls), 5)
        proposals = result["proposals"]
        self.assertEqual([p["kind"] for p in proposals], ["hypothesis", "hypothesis", "evidence_gap"])
        self.assertEqual(proposals[2]["hypothesis_proposal_ids"], [p["proposal_id"] for p in proposals[:2]])
        self.assertTrue(all(not p["evidence_refs"] for p in proposals))
        self.assertFalse(any(t["name"] == "get_evidence" for t in self.audits()[0]["tool_calls"]))

    def test_source_required_proposals_reject_empty_and_unobtained_refs_then_recover(self):
        context = self.context()
        context["reasoning"]["hypotheses"] = [{"id": "formal-hypothesis", "content": "存在租金损失", "revision": 1}]
        for name in ("propose_evidence_link", "propose_hypothesis_status"):
            with self.subTest(name=name):
                common = {"hypothesis_id": "formal-hypothesis", "rationale": "核对陈述的具体依据", "evidence_refs": []}
                if name == "propose_evidence_link":
                    common.update(info_unit_id="U029", meaning="verify", hypothesis_proposal_ids=[])
                else:
                    common.update(info_unit_ids=["U029"], judgment="unverified")
                cited = {**common, "evidence_refs": [{"info_unit_id": "U029", "source_ref_index": 0}]}
                result, calls = self.run_script("核对假设依据", [f.tool("search_info_units", {"query": "损失"}),
                    f.tool(name, common, "empty-ref"), f.tool(name, cited, "unobtained-ref"),
                    f.tool("get_evidence", {"info_unit_ids": ["U029"]}, "source"),
                    f.tool(name, cited, "valid-ref"), f.finish("核对假设依据", status="completed")],
                    work_context=context, issue_key=self.case.issue_set[1])
                missing = json.loads(calls[2]["messages"][1]["content"])["recovery"]
                unobtained = json.loads(calls[3]["messages"][1]["content"])["recovery"]
                self.assertEqual(missing["code"], "invalid_tool_arguments")
                self.assertEqual(unobtained["code"], "invalid_investigation_reference")
                self.assertEqual(len(result["proposals"]), 1)
                self.assertEqual(result["proposals"][0]["evidence_refs"], cited["evidence_refs"])

    def test_generations_and_audits_track_exact_single_prompt_and_request(self):
        backend = tracing.FakeBackend()
        with patch.object(observability, "load_settings", return_value=tracing.SETTINGS), patch.object(observability, "_get_backend", return_value=backend):
            result, calls = self.run_script("核对陈述", [f.tool("search_info_units", {"query": "损失"}, "search"),
                f.tool("get_evidence", {"info_unit_ids": ["U029"]}),
                f.finish("核对陈述", ["U029"], refs=[("U029", 0)])])
        audit = self.audits()[0]
        prompt = load_prompt(agent.PROMPT_PATH, agent.PROMPT_NAME)
        generations = [s for s in backend.client.spans if s.start.get("as_type") == "generation"]
        self.assertEqual(len(generations), 3)
        for recorded, span, call in zip(audit["model_calls"], generations, calls):
            self.assertEqual(recorded["request"], span.start["input"])
            self.assertEqual(call["messages"][0]["content"], prompt.content)
            for key, value in prompt.metadata(f.CONFIG.model).items():
                self.assertEqual(recorded[key], value)
                self.assertEqual(span.start["metadata"][key], value)
            self.assertNotIn("phase", recorded)
        self.assertEqual(audit["information_acquisition"]["obtained_source_count"], 2)
        spans = [s for s in backend.client.spans if s.start.get("as_type") == "tool"]
        for recorded, span in zip(audit["tool_calls"], spans):
            metadata = next(u["metadata"] for u in span.updates if "read_progress" in u.get("metadata", {}))
            for key in ("read_progress", "no_progress"):
                self.assertEqual(metadata[key], recorded[key])
            if recorded["name"] == "search_info_units":
                self.assertEqual(metadata["excluded_info_unit_ids"], recorded["excluded_info_unit_ids"])
