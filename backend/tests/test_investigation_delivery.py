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

    def test_single_proposition_and_gap_keep_compact_memory_and_full_audit(self):
        goal = "核查是否存在少收租金及尚待核实的事实"
        proposition = "新租约延后计租，房东存在少收租金的期间。"
        def respond(call, turn):
            state = json.loads(call["messages"][1]["content"])
            names = {d["function"]["name"] for d in call["definitions"]}
            self.assertNotIn("propose_hypothesis_status", names)
            self.assertNotIn("propose_evidence_gap_update", names)
            self.assertNotIn("search_evidence_gaps", names)
            self.assertEqual([m["role"] for m in call["messages"]], ["system", "user"])
            if turn == 1:
                self.assertNotIn("propose_evidence_link", names)
                return f.tool("search_info_units", {"query": "损失"}, "read")
            visible = {u["id"] for u in state["materials"]["info_units"]["items"]}
            self.assertTrue({"U029", "U036"} <= visible)
            self.assertFalse(visible & {u["id"] for u in state["case_info_directory"]["items"]})
            if turn == 2:
                return f.tool("propose_hypothesis", {"content": proposition,
                    "rationale": "房东称空置造成损失；是否少收租金需查计租和收款记录。",
                    "info_unit_ids": ["U029", "U036"], "evidence_refs": []}, "one-hypothesis")
            self.assertIn("propose_evidence_link", names)
            hypothesis = state["proposals"][0]
            self.assertEqual(set(hypothesis), {"proposal_id", "kind", "content", "rationale", "info_unit_ids"})
            self.assertEqual(hypothesis["content"], proposition)
            if turn == 3:
                return f.tool("propose_evidence_gap", {"content": "腾退后的租金是否足额收取？",
                    "rationale": "核对银行入账与计租期间。", "basis_info_unit_ids": ["U036"], "evidence_refs": [],
                    "hypothesis_targets": {"hypothesis_ids": [], "hypothesis_proposal_ids": [hypothesis["proposal_id"]]}}, "one-gap")
            return f.finish(goal, status="evidence_insufficient", questions=["实际收款仍待核实。"],
                            answer="现有陈述不足以确认少收租金，已提交待验证命题和收款核查问题。")
        result, calls = self.run_script(goal, respond, work_context=self.context(), issue_key=self.case.issue_set[1])
        self.assertEqual([p["kind"] for p in result["proposals"]], ["hypothesis", "evidence_gap"])
        self.assertEqual(result["proposals"][1]["hypothesis_proposal_ids"], [result["proposals"][0]["proposal_id"]])
        self.assertEqual(result["summary_status"], "completed")
        audit = self.audits()[0]
        # Projection changes neither the adoption contract nor the exact request audit.
        self.assertEqual(audit["result"]["proposals"], result["proposals"])
        self.assertEqual(result["proposals"][0]["evidence_refs"], [])
        for call, recorded in zip(calls, audit["model_calls"]):
            self.assertEqual(recorded["request"]["messages"], call["messages"])

    def test_two_distinct_hypotheses_and_shared_gap_use_real_returned_dependencies(self):
        goal = "读取双方材料，判断收款是否连续以及是否发生少收租金"
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
                    "hypothesis_targets": {"hypothesis_ids": [], "hypothesis_proposal_ids": [p["proposal_id"] for p in proposals]},
                    "rationale": "收款与计租记录可以区分是否实际少收租金。", "basis_info_unit_ids": [], "evidence_refs": []}, "gap")
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

    def test_independent_propositions_share_a_factual_gap_without_requiring_competition(self):
        goal = "根据损失陈述梳理需要核实的命题和事实"
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
                    "content": "若新租约延后计租且该期间无其他收款，则存在少收租金。" if not proposals else "若新租约租金降低且没有差额补偿，则后续租金收入减少。",
                    "rationale": "损失主张需要核对相应的计租或实收条件，尚未核验原文。",
                    "info_unit_ids": ["U029", "U036"], "evidence_refs": []}, f"hyp-{len(proposals)}")
            if len(proposals) == 2:
                return f.tool("propose_evidence_gap", {"content": "腾退后的实际收款是否覆盖原计租期间应收的金额？",
                    "rationale": "对照原租约应收与实际收款，可核查计租断档和租金降低这两个独立命题。",
                    "basis_info_unit_ids": ["U029", "U036"], "evidence_refs": [],
                    "hypothesis_targets": {"hypothesis_ids": [], "hypothesis_proposal_ids": [p["proposal_id"] for p in proposals]}}, "gap")
            return f.finish(goal, status="completed", answer="已提交两个可分别检验的命题及一个共同核查问题，请审阅。")
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
        for name in ("propose_evidence_link", "propose_hypothesis_status"):
            with self.subTest(name=name):
                from app.reasoning_service import edit_reasoning
                from app.reasoning_models import ReasoningEdit, HypothesisEdit
                _, hid = edit_reasoning(self.case, ReasoningEdit(issue=self.case.issue_set[1], hypothesis=HypothesisEdit(content=f"存在租金损失 {name}")))
                context["reasoning"]["hypotheses"] = [{"id": hid, "content": "存在租金损失", "revision": 1}]
                common = {"hypothesis_id": hid, "rationale": "核对陈述的具体依据", "evidence_refs": []}
                if name == "propose_evidence_link":
                    common.update(info_unit_id="U029", meaning="related", hypothesis_proposal_ids=[])
                else:
                    common.update(info_unit_ids=["U029"], judgment="verified")
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
