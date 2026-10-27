"""Entry gating and composed Prompt provenance at the actual runtime boundary."""
import hashlib
import json
import unittest
from unittest.mock import patch

import test_investigation_agent as f
import test_observability as tracing
from app import investigation_agent as agent, observability
from app.investigation_state import QuestionState
from app.prompt_loader import load_composed_prompt


def direct(answer="当前案件共有17个信息单元。", call_id="direct", **changes):
    return f.tool("finish_investigation", {"answer": answer, "findings": [],
        "question_updates": [], "step_updates": [], "completion_status": "completed", **changes}, call_id)


def plan(questions=None, steps=None, ident="plan"):
    return f.tool("update_investigation_steps", {
        "questions": questions if questions is not None else [f.question("笔录对入住日期如何记载", "date")],
        "steps": steps if steps is not None else [f.step("read", "date", objective="核对入住日期来源")]}, ident)


class TestInvestigationEntry(unittest.TestCase):
    setUp = f.TestInvestigationAgent.setUp
    tearDown = f.TestInvestigationAgent.tearDown
    run_script = f.TestInvestigationAgent.run_script
    audits = f.TestInvestigationAgent.audits

    def test_simple_requests_finish_in_entry_without_questions_steps_or_findings(self):
        for goal, answer in (("一共有多少个InfoUnit", "当前案件共有17个信息单元。"),
                             ("你能做什么", "我可以检索案件信息、核对原始来源，并提出供你审核的推理建议。"),
                             ("当前争议是什么", "当前争议是提前退租后押金是否应退还。")):
            with self.subTest(goal=goal):
                result, calls = self.run_script(goal, [direct(answer)])
                self.assertEqual(result["answer"], answer)
                self.assertEqual(result["phase"], "entry")
                self.assertEqual(result["completion_status"], "completed")
                self.assertEqual(result["question_state"]["questions"], [])
                self.assertEqual(result["question_state"]["investigation_steps"], [])
                self.assertEqual(result["findings"], [])
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0]["tool_choice"], "required")
                self.assertEqual({d["function"]["name"] for d in calls[0]["definitions"]},
                    {"finish_investigation", "update_investigation_steps"})
                state = json.loads(calls[0]["messages"][1]["content"])
                self.assertEqual(state["question_state"]["current_question_ids"], [])
                self.assertEqual(state["information_acquisition"]["status"], "not_planned")
                self.assertFalse(state["information_acquisition"]["needs_initial_read"])

    def test_plan_switches_phase_and_preserves_goal_through_investigation(self):
        goal = "核对入住日期，并保留无法确认的实际损失"
        questions = [f.question("入住日期的记载", "date", status="in_progress"), f.question("实际损失能否确认", "loss")]
        steps = [f.step("read", "date", objective="核对入住日期来源"), f.step("loss-read", "loss", objective="核对损失判断条件")]
        result, calls = self.run_script(goal, [plan(questions, steps),
            f.tool("get_evidence", {"info_unit_ids": ["U026"], "step_id": "read"}, "source"),
            f.finish(goal, ["U026"], refs=[("U026", 0)], status="evidence_insufficient", updates=[
                f.question("入住日期的记载", "date", status="answered", units=["U026"], refs=[("U026", 0)], finding="张某称3月8日入住，尚未独立核实。"),
                f.question("实际损失能否确认", "loss", status="blocked", reason="缺少实际计租与收款条件", block_kind="evidence_missing")],
                step_updates=[f.step("read", "date", status="completed", objective="核对入住日期来源", note="已核对来源记载"),
                    f.step("loss-read", "loss", status="deferred", objective="核对损失判断条件", note="当前未取得实际收款与计租条件，保留待核查")])])
        self.assertEqual(result["phase"], "execution")
        self.assertEqual([q["status"] for q in result["question_state"]["questions"]], ["answered", "blocked"])
        for i, call in enumerate(calls):
            state = json.loads(call["messages"][1]["content"])
            self.assertEqual(state["goal"], goal)
            self.assertEqual(state["phase"], "entry" if i == 0 else "execution")
            self.assertEqual(call["messages"][0]["content"], load_composed_prompt(agent.PROMPT_NAMES[state["phase"]]).content)
        self.assertEqual(len(calls[1]["definitions"]), 3)
        self.assertEqual(calls[1]["tool_choice"], "required")
        state = json.loads(calls[1]["messages"][1]["content"])
        self.assertEqual(state["question_state"]["current_question_ids"], ["date"])
        self.assertEqual([s["id"] for s in state["investigation_steps"]["progress"]], ["read", "loss-read"])

    def test_invalid_plan_is_atomic_and_remains_entry_until_valid_submission(self):
        bad = [f.step("read", "missing")]
        result, calls = self.run_script("核查", [plan(steps=bad), plan(ident="fixed"),
            f.tool("search_info_units", {"query": "独立入住记录", "step_id": "read"}, "search"),
            f.finish("核查", status="evidence_insufficient", updates=[f.question("笔录对入住日期如何记载", "date", status="blocked",
                reason="本次尚未取得有效来源", block_kind="evidence_missing")], step_updates=[
                f.step("read", "date", status="deferred", objective="核对入住日期来源", note="尚未取得来源，保留待核查")])])
        state = json.loads(calls[1]["messages"][1]["content"])
        self.assertEqual(state["phase"], "entry")
        self.assertEqual(state["question_state"]["progress"], [])
        self.assertEqual(state["investigation_steps"]["progress"], [])
        self.assertIsNotNone(state["recovery"])
        self.assertEqual(result["phase"], "execution")

    def test_failed_direct_finish_stays_entry_and_can_correct_answer(self):
        bad = direct("现有输入不足", findings=[{"statement": "无来源断言", "info_unit_ids": [], "relation_ids": [], "evidence_refs": []}])
        result, calls = self.run_script("核查", [bad, direct("当前输入不足以支持事实判断。", call_id="fixed")])
        state = json.loads(calls[1]["messages"][1]["content"])
        self.assertEqual(state["phase"], "entry")
        self.assertEqual(state["recovery"]["code"], "invalid_entry_finish")
        self.assertEqual(state["question_state"]["progress"], [])
        self.assertEqual(result["phase"], "entry")

    def test_failed_direct_finish_can_reconsider_and_submit_an_investigation_plan(self):
        result, calls = self.run_script("核查事实", [direct(completion_status="evidence_insufficient"), plan(),
            f.tool("search_info_units", {"query": "独立入住记录", "step_id": "read"}, "search"),
            f.finish("核查事实", status="evidence_insufficient", updates=[f.question("笔录对入住日期如何记载", "date", status="blocked",
                reason="尚未取得可核验来源", block_kind="evidence_missing")], step_updates=[
                    f.step("read", "date", status="deferred", objective="核对入住日期来源", note="尚未取得来源，保留待核查")])])
        self.assertEqual([json.loads(c["messages"][1]["content"])["phase"] for c in calls], ["entry", "entry", "execution", "execution"])
        self.assertEqual(self.audits()[0]["tool_calls"][1]["phase_after"], "execution")
        self.assertEqual(result["phase"], "execution")

    def test_hidden_tools_never_dispatch_and_plain_text_does_not_finish(self):
        with patch.object(agent.tools, "search_info_units", side_effect=AssertionError("must not execute")):
            result, calls = self.run_script("说明能力", [f.tool("search_info_units", {"query": "损失"}),
                f.final("普通文本不能结束"), direct("我可以核对来源并整理待审核建议。")])
        self.assertTrue(all(c["tool_choice"] == "required" for c in calls))
        self.assertEqual(json.loads(calls[1]["messages"][1]["content"])["recovery"]["code"], "unknown_tool")
        self.assertEqual(json.loads(calls[2]["messages"][1]["content"])["recovery"]["code"], "finish_required")
        self.assertIn("update_investigation_steps", json.loads(calls[2]["messages"][1]["content"])["recovery"]["message"])
        self.assertEqual(result["model_call_count"], 3)

    def test_question_ids_have_no_special_root_identity_and_focus_can_change(self):
        state = QuestionState()
        self.assertEqual(state.dump()["questions"], [])
        seen = {"units": set(), "relations": set(), "evidence": set()}
        state.update([f.question("日期", "Q1"), f.question("金额", "amount", status="in_progress")], self.case, seen, None)
        self.assertEqual(state.resolve(None), ["amount"])
        state.update([f.question("重新界定日期条件", "Q1", status="in_progress"), f.question("金额", "amount")], self.case, seen, None)
        self.assertEqual(state.resolve(None), ["Q1"])
        self.assertEqual(state.items["Q1"].question, "重新界定日期条件")

    def test_audit_and_langfuse_share_phase_and_composition_metadata(self):
        backend = tracing.FakeBackend()
        settings = observability.ObservabilitySettings(True, True, "pk-test", "sk-test", "https://test", "test")
        with patch.object(observability, "load_settings", return_value=settings), patch.object(observability, "_get_backend", return_value=backend):
            self.run_script("数量", [direct()])
        audit = self.audits()[0]
        call = audit["model_calls"][0]
        generation = next(s for s in backend.client.spans if s.start.get("as_type") == "generation")
        self.assertEqual(call["phase"], "entry")
        metadata = {k: call[k] for k in audit["prompt_manifest"]["entry"]}
        self.assertEqual(metadata, {k: generation.start["metadata"][k] for k in metadata})
        self.assertEqual(generation.start["metadata"]["tool_policy"], "entry_decision")
        self.assertEqual(call["prompt_sha256"], hashlib.sha256(call["request"]["messages"][0]["content"].encode()).hexdigest())
        self.assertEqual([p["prompt_name"] for p in metadata["prompt_components"]], ["investigation_common", "investigation_entry"])
        self.assertEqual(audit["tool_calls"][0]["phase"], "entry")
        self.assertEqual(audit["tool_calls"][0]["phase_after"], "entry")
        paths = list((self.data_dir / self.case.case_id / "runs").glob("*.input.json"))
        snapshot = json.loads(paths[0].read_text(encoding="utf-8"))
        for phase, row in snapshot["system_prompts"].items():
            self.assertEqual(row["prompt_sha256"], hashlib.sha256(row["content"].encode()).hexdigest())
            self.assertEqual(row["prompt_version"], audit["prompt_manifest"][phase]["prompt_version"])
