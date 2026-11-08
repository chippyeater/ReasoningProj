"""Harness feedback must reach actual next model requests, entirely offline."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

import test_investigation_agent as f
import test_reasoning_proposals as fixture
from app import investigation_agent as agent, investigation_actions as actions, investigation_tools as tools
from app import investigation_state as state, reasoning_proposals
from app.errors import AppError
from app.issue_workspaces import investigation_context


class TestInvestigationToolFeedback(unittest.TestCase):
    setUp = fixture.TestReasoningProposals.setUp
    tearDown = fixture.TestReasoningProposals.tearDown
    edit = fixture.TestReasoningProposals.edit
    hypothesis = fixture.TestReasoningProposals.hypothesis
    run_script = f.TestInvestigationAgent.run_script
    audits = f.TestInvestigationAgent.audits

    @staticmethod
    def view(call):
        return json.loads(call["messages"][1]["content"])

    def hypothesis_call(self, index, **values):
        return f.tool("propose_hypothesis", {"content": f"独立待核实命题 {index}", "rationale": "探索性命题。",
            "info_unit_ids": [], **values}, f"hyp-{index}")

    def no_change_run(self, *, continue_work):
        # Minimal reproduction of eefc1249294218bcdb7294d82516f8fe: an open
        # Gap is updated, then the same material/status is resubmitted with a
        # changed rationale. The second result is no_change, not an exception.
        hid = self.hypothesis()
        gid = self.edit(gap={"content": "指定期间的收款是否连续？", "hypothesis_ids": [hid],
            "basis_info_unit_ids": ["U026"]}).json()["object_id"]
        update = {"gap_id": gid, "addressing_info_unit_ids": ["U029"], "status": "open", "rationale": "仅部分回应。"}
        replies = [f.tool("propose_evidence_gap_update", update, "update"),
            f.tool("propose_evidence_gap_update", {**update, "rationale": "仅改写同一操作的理由。"}, "noop")]
        if continue_work:
            replies.append(self.hypothesis_call("after-feedback", info_unit_ids=["U029"]))
        replies.append(f.finish("核查收款", status="completed"))
        with patch.object(reasoning_proposals, "persist_issue_workspaces", wraps=reasoning_proposals.persist_issue_workspaces) as save:
            result, calls = self.run_script("核查收款", replies, selected=["U029"], issue_key=self.issue,
                work_context=investigation_context(self.case, self.issue, selected_reasoning_ids=[gid]))
        feedback = self.view(calls[2])["previous_tool_result"]["recovery"]
        audited = self.audits()[0]["tool_calls"][1]["result"]
        self.assertEqual(feedback["tool"], "propose_evidence_gap_update")
        self.assertEqual(feedback["status"], "ok")
        self.assertTrue(feedback["no_change"])
        self.assertFalse(feedback["applied"])
        self.assertEqual(feedback["message"], audited["message"])
        self.assertEqual(feedback["arguments"]["gap_id"], gid)
        for instruction in ("status is unchanged", "already associated", "rationale", "finish_investigation", "Do not resubmit"):
            self.assertIn(instruction, feedback["message"])
        self.assertEqual(result["workspace_commit"]["status"], "applied")
        self.assertEqual(result["tool_budget_used"]["proposal"], 3 if continue_work else 2)
        self.assertEqual(len(result["proposals"]), 2 if continue_work else 1)
        self.assertEqual(save.call_count, 1)
        if continue_work:
            self.assertIsNone(self.view(calls[3])["previous_tool_result"]["recovery"])

    def test_gap_update_no_change_message_reaches_finish_request(self):
        self.no_change_run(continue_work=False)

    def test_no_change_feedback_can_be_followed_by_work_and_then_clears(self):
        self.no_change_run(continue_work=True)

    def test_all_ten_tools_deliver_argument_parse_errors_to_the_next_request(self):
        for definition in agent.tool_definitions():
            name, schema = definition["function"]["name"], definition["function"]["parameters"]
            args = {field: None for field in schema["required"]}
            args["unexpected_field"] = True
            with self.subTest(tool=name):
                result, calls = self.run_script("检查工具调用", [f.tool(name, args, "invalid"),
                    f.finish("检查工具调用", status="completed", basis="general")], issue_key=self.issue)
                feedback = self.view(calls[1])["previous_tool_result"]["recovery"]
                self.assertEqual(feedback["tool"], name)
                self.assertEqual(feedback["status"], "invalid_request")
                error = feedback["validation_details"]["errors"][0]
                self.assertEqual(error["argument_loc"], ["unexpected_field"])
                self.assertEqual(error["code"], "extra_forbidden")
                self.assertTrue(error["fix"])
                self.assertTrue(feedback["next_action"]["steps"])
                self.assertEqual(result["summary_status"], "completed")

    def test_five_proposal_validation_paths_deliver_fields_and_dependency_repairs(self):
        hid = self.hypothesis()
        gid = self.edit(gap={"content": "回应材料是否充分？", "hypothesis_ids": [hid]}).json()["object_id"]
        context = investigation_context(self.case, self.issue, selected_reasoning_ids=[gid])
        cases = [
            ("propose_hypothesis", {"content": " ", "rationale": "需核验。", "info_unit_ids": []}, ["content"]),
            ("propose_evidence_link", {"hypothesis_id": hid, "hypothesis_proposal_ids": [], "info_unit_id": "U029",
                "meaning": "invented", "rationale": "错误枚举。"}, ["meaning"]),
            ("propose_evidence_gap", {"content": "待核实问题", "rationale": "缺少依赖对象。", "basis_info_unit_ids": [],
                "hypothesis_targets": {"hypothesis_ids": [], "hypothesis_proposal_ids": ["proposal-" + "f" * 32]}},
                ["hypothesis_targets", "hypothesis_proposal_ids", 0]),
            ("propose_hypothesis_status", {"hypothesis_id": "hyp-missing", "judgment": "verified",
                "rationale": "目标不存在。", "info_unit_ids": ["U029"]}, ["hypothesis_id"]),
            ("propose_evidence_gap_update", {"gap_id": "gap-missing", "status": "open",
                "addressing_info_unit_ids": ["U029"], "rationale": "目标不存在。"}, ["gap_id"]),
        ]
        for name, args, loc in cases:
            with self.subTest(tool=name):
                result, calls = self.run_script("核对参数", [f.tool(name, args),
                    f.finish("核对参数", status="completed", basis="general")], selected=["U029"],
                    issue_key=self.issue, work_context=context)
                feedback = self.view(calls[1])["previous_tool_result"]["recovery"]
                self.assertEqual(feedback["status"], "invalid_request")
                self.assertTrue(any(e["argument_loc"] == loc for e in feedback["validation_details"]["errors"]))
                self.assertTrue(all(e["message"] and e["fix"] for e in feedback["validation_details"]["errors"]))
                self.assertEqual(result["proposals"], [])

    def test_proposal_staging_failure_keeps_its_supplied_repair(self):
        failure = AppError("staging_rejected", "The staged batch cannot be applied", details={"errors": [{
            "loc": ["rationale"], "code": "staging_rejected", "message": "Staging failed before any write",
            "fix": "Correct this pending edit and resubmit the indicated tool."}]})
        with patch.object(reasoning_proposals, "apply_proposal", side_effect=failure):
            result, calls = self.run_script("核对暂存", [self.hypothesis_call(1),
                f.finish("核对暂存", status="completed", basis="general")], issue_key=self.issue)
        feedback = self.view(calls[1])["previous_tool_result"]["recovery"]
        self.assertEqual(feedback["code"], "staging_rejected")
        self.assertIn("Correct this pending edit", feedback["next_action"]["steps"][0]["instruction"])
        self.assertEqual(result["proposals"], [])

    def test_invalid_json_diagnostics_and_raw_call_are_visible(self):
        raw = '{"info_unit_id":'
        _, calls = self.run_script("检查 JSON", [f.tool("get_evidence", raw),
            f.finish("检查 JSON", status="completed", basis="general")])
        feedback = self.view(calls[1])["previous_tool_result"]["recovery"]
        self.assertEqual(feedback["arguments"]["raw"], raw)
        error = feedback["validation_details"]["errors"][0]
        self.assertEqual(error["code"], "invalid_json")
        self.assertIn("lineno", error)
        self.assertTrue(error["fix"])

    def test_read_parameter_error_clears_after_a_successful_read_without_stale_error_notices(self):
        _, calls = self.run_script("核查", [f.tool("get_evidence", {"info_unit_id": "U029", "offset": -1}, "bad"),
            f.tool("get_evidence", {"info_unit_id": "U029"}, "good"), f.finish("核查", status="completed")])
        feedback = self.view(calls[1])["previous_tool_result"]["recovery"]
        self.assertEqual(feedback["code"], "invalid_offset")
        self.assertEqual(feedback["arguments"]["offset"], -1)
        self.assertEqual(feedback["validation_details"]["errors"][0]["argument_loc"], ["offset"])
        self.assertTrue(feedback["validation_details"]["errors"][0]["fix"])
        self.assertIsNone(self.view(calls[2])["previous_tool_result"]["recovery"])
        self.assertEqual(self.view(calls[2])["previous_tool_result"]["notices"], [])

    def test_successful_tool_guidance_is_delivered_without_becoming_an_error(self):
        original = tools.search_info_units
        def search(case, **args):
            result = original(case, **args)
            result.update(message="Optional source context is available for this result.",
                fix="Use the returned InfoUnit when further source detail is needed.",
                next_action={"steps": [{"tool": "get_evidence", "arguments": {"info_unit_id": "U004"},
                    "instruction": "Read original wording only if needed."}]})
            return result
        with patch.object(tools, "search_info_units", side_effect=search):
            _, calls = self.run_script("读取反馈", [f.tool("search_info_units", {"query": "月租"}, "hint"),
                f.tool("get_evidence", {"info_unit_id": "U004"}, "source"), f.finish("读取反馈", status="completed")])
        feedback = self.view(calls[1])["previous_tool_result"]["recovery"]
        self.assertEqual(feedback["status"], "ok")
        self.assertEqual(feedback["message"], "Optional source context is available for this result.")
        self.assertEqual(feedback["next_action"]["steps"][0]["tool"], "get_evidence")
        self.assertIn("returned InfoUnit", feedback["fix"])
        self.assertIsNone(self.view(calls[2])["previous_tool_result"]["recovery"])

    def test_duplicate_creation_is_checked_against_current_working_objects(self):
        first = self.hypothesis_call(1)
        second = deepcopy(first)
        second.message["tool_calls"][0]["id"] = "repeated-call"
        third = self.hypothesis_call(1, rationale="同一命题的新理由。")
        third.message["tool_calls"][0]["id"] = "same-proposition"
        _, calls = self.run_script("比较重复调用", [first, second,
            third,
            f.finish("比较重复调用", status="completed", basis="general")], issue_key=self.issue)
        duplicate = self.view(calls[2])["previous_tool_result"]["recovery"]
        self.assertEqual(duplicate["code"], "duplicate_proposal")
        semantic = self.view(calls[3])["previous_tool_result"]["recovery"]
        self.assertEqual(semantic["code"], "duplicate_proposal")
        obj = semantic["next_action"]["steps"][0]["existing_object"]
        self.assertEqual(obj["id"], self.view(calls[1])["previous_tool_result"]["result"]["object_id"])

    def test_all_four_read_execution_exceptions_reach_the_next_request(self):
        args = {"search_info_units": {"query": "月租"}, "get_relations": {"info_unit_ids": ["U029"]},
                "get_evidence": {"info_unit_id": "U029"},
                "search_evidence_gaps": {"query": None, "info_unit_ids": None, "limit": 3, "offset": 0}}
        for name, values in args.items():
            with self.subTest(tool=name), patch.object(tools, name, side_effect=OSError("cache unavailable")):
                _, calls = self.run_script("报告工具问题", [f.tool(name, values),
                    f.finish("报告工具问题", status="completed", basis="general")], issue_key=self.issue)
                feedback = self.view(calls[1])["previous_tool_result"]["recovery"]
                self.assertEqual(feedback["status"], "error")
                self.assertEqual(feedback["code"], "investigation_tool_failed")
                self.assertIn("Tool failed", feedback["message"])
                self.assertIn("Do not unconditionally repeat", feedback["next_action"]["steps"][0]["instruction"])

    def test_gap_search_preserves_field_details_returned_inside_a_read_failure(self):
        _, calls = self.run_script("检查分页", [f.tool("search_evidence_gaps", {
            "query": None, "info_unit_ids": None, "limit": 3, "offset": -1}),
            f.finish("检查分页", status="completed", basis="general")], issue_key=self.issue)
        feedback = self.view(calls[1])["previous_tool_result"]["recovery"]
        self.assertEqual(feedback["code"], "invalid_tool_arguments")
        self.assertEqual(feedback["validation_details"]["errors"][0]["argument_loc"], ["offset"])
        self.assertIn("greater than or equal to zero", feedback["next_action"]["steps"][0]["instruction"])

    def test_cached_source_validation_failure_keeps_the_actual_reason(self):
        case = self.case.model_copy(deep=True)
        next(u for u in case.info_units if u.id == "U029").source_refs[0].quote += "错误转写"
        result, calls = self.run_script("核查来源", [f.tool("get_evidence", {"info_unit_id": "U029"}),
            f.finish("核查来源")], case=case, selected=["U029"])
        feedback = self.view(calls[1])["previous_tool_result"]["recovery"]
        self.assertEqual(feedback["code"], "source_quote_mismatch")
        self.assertIn("cached TXT", feedback["message"])
        self.assertEqual(feedback["arguments"]["info_unit_id"], "U029")
        self.assertTrue(feedback["recovery_hint"])
        self.assertEqual(result["evidence_refs"], [])

    def test_last_budget_failure_and_closure_are_both_present_in_the_next_request(self):
        replies = [self.hypothesis_call(i) for i in range(6)]
        replies += [f.tool("search_info_units", {"query": "月租"}, "read"),
                    f.tool("get_evidence", {"info_unit_id": "U029", "limit": 13}, "last-invalid"),
                    f.finish("核查", status="budget_exhausted")]
        result, calls = self.run_script("核查", replies, issue_key=self.issue)
        feedback = self.view(calls[-1])["previous_tool_result"]["recovery"]
        self.assertEqual(feedback["code"], "invalid_limit")
        self.assertEqual(feedback["validation_details"]["errors"][0]["argument_loc"], ["limit"])
        self.assertEqual(feedback["constraints"]["code"], "tool_budget_exhausted")
        self.assertIn("budget is exhausted", feedback["constraints"]["message"])
        self.assertEqual(feedback["constraints"]["allowed_tools"], ["finish_investigation"])
        step = feedback["next_action"]["steps"][0]
        self.assertEqual(step["tool"], "finish_investigation")
        self.assertEqual(step["unavailable_tool"], "get_evidence")
        self.assertNotIn("Correct ['limit']", step["instruction"])
        self.assertEqual(result["tool_budget_used"], {"read": 2, "proposal": 6, "finish": 1})
        self.assertEqual(result["workspace_commit"]["status"], "applied")

    def test_no_change_on_the_last_attempt_keeps_its_message_and_budget_constraint(self):
        hid = self.hypothesis()
        gid = self.edit(gap={"content": "指定期间的收款条件是什么？", "hypothesis_ids": [hid]}).json()["object_id"]
        values = {"gap_id": gid, "status": "open", "addressing_info_unit_ids": ["U029"], "rationale": "部分回应。"}
        replies = [self.hypothesis_call(i) for i in range(4)]
        replies += [f.tool("get_evidence", {"info_unit_id": "U029", "limit": 1}, "page-0"),
            f.tool("get_evidence", {"info_unit_id": "U029", "offset": 1, "limit": 1}, "page-1"),
            f.tool("propose_evidence_gap_update", values, "update"),
            f.tool("propose_evidence_gap_update", {**values, "rationale": "相同材料与状态，只有理由变化。"}, "noop"),
            f.finish("核查", status="budget_exhausted")]
        result, calls = self.run_script("核查", replies, issue_key=self.issue,
            work_context=investigation_context(self.case, self.issue, selected_reasoning_ids=[gid]))
        feedback = self.view(calls[-1])["previous_tool_result"]["recovery"]
        self.assertTrue(feedback["no_change"])
        self.assertEqual(feedback["status"], "ok")
        self.assertIn("Rewording rationale alone", feedback["message"])
        self.assertEqual(feedback["constraints"]["code"], "tool_budget_exhausted")
        self.assertEqual(result["tool_budget_used"], {"proposal": 6, "read": 2, "finish": 1})
        self.assertEqual(len(result["proposals"]), 5)
        self.assertEqual(result["workspace_commit"]["status"], "applied")

    def test_budget_closure_constrains_an_exception_repair_without_an_explicit_tool(self):
        replies = [self.hypothesis_call(i) for i in range(6)]
        replies += [f.tool("search_info_units", {"query": "月租"}, "read"),
                    f.tool("get_evidence", {"info_unit_id": "U029"}, "crashed"),
                    f.finish("核查", status="budget_exhausted")]
        with patch.object(tools, "get_evidence", side_effect=OSError("read failed")):
            _, calls = self.run_script("核查", replies, issue_key=self.issue)
        feedback = self.view(calls[-1])["previous_tool_result"]["recovery"]
        self.assertEqual(feedback["code"], "investigation_tool_failed")
        step = feedback["next_action"]["steps"][0]
        self.assertEqual(step["tool"], "finish_investigation")
        self.assertEqual(step["unavailable_tool"], "get_evidence")
        self.assertIn("open_questions", step["instruction"])

    def test_no_progress_closure_does_not_hide_the_last_unknown_info_unit(self):
        replies = [f.tool("search_info_units", {"query": f"不存在材料{i}"}, f"empty-{i}") for i in range(2)]
        replies += [f.tool("get_evidence", {"info_unit_id": "foreign"}, "invalid"), f.finish("核查")]
        _, calls = self.run_script("核查", replies)
        feedback = self.view(calls[-1])["previous_tool_result"]["recovery"]
        self.assertEqual(feedback["code"], "unknown_info_unit")
        self.assertEqual(feedback["constraints"]["code"], "no_progress_limit")
        self.assertEqual(feedback["validation_details"]["errors"][0]["info_unit_id"], "foreign")
        self.assertIn("original_fix", feedback["validation_details"]["errors"][0])
        self.assertNotIn("unavailable", feedback["constraints"]["message"].lower())

    def test_non_tool_reply_adds_finish_required_without_erasing_the_previous_error(self):
        _, calls = self.run_script("核查", [f.tool("get_evidence", {"info_unit_id": "foreign"}),
            f.final("普通文本不能完成调查。"), f.finish("核查", status="completed", basis="general")])
        feedback = self.view(calls[2])["previous_tool_result"]["recovery"]
        self.assertEqual(feedback["code"], "unknown_info_unit")
        self.assertEqual(feedback["constraints"]["code"], "finish_required")
        self.assertEqual(calls[2]["tool_choice"], "required")

    def test_finish_validation_failure_can_be_fixed_with_the_actual_next_request(self):
        result, calls = self.run_script("核查", [f.tool("get_evidence", {"info_unit_id": "U029"}),
            f.tool("finish_investigation", {"answer": "证据不足。", "answer_basis": "case_material",
                "open_questions": [], "completion_status": "evidence_insufficient"}, "bad-finish"),
            f.finish("核查")])
        feedback = self.view(calls[2])["previous_tool_result"]["recovery"]
        self.assertEqual(feedback["code"], "missing_evidence_limitation")
        self.assertEqual(feedback["validation_details"]["errors"][0]["argument_loc"], ["open_questions"])
        self.assertIn("missing critical evidence", feedback["validation_details"]["errors"][0]["fix"])
        self.assertEqual(result["tool_budget_used"]["finish"], 2)

    def test_oversized_execution_is_rejected_explicitly_without_summarizing_originals(self):
        errors = [{"loc": ["rationale"], "code": "value_error", "message": "x" * 9000,
                   "fix": "Use obtained IDs"} for _ in range(4)]
        failure = AppError("invalid_reasoning_proposal", "Detailed failure", details={"errors": errors})
        with patch.object(actions, "_propose", side_effect=failure), self.assertRaises(AppError) as caught:
            self.run_script("检查长错误", [self.hypothesis_call(1)], issue_key=self.issue)
        self.assertEqual(caught.exception.code, "investigation_context_limit")
        original = self.audits()[0]["tool_calls"][0]["result"]["error"]["validation_details"]["errors"]
        self.assertEqual(len(original), 4)
        self.assertEqual(original[0]["message"], "x" * 9000)

    def test_empty_and_truncated_reads_remain_informational_not_error_feedback(self):
        _, calls = self.run_script("查找材料", [f.tool("search_info_units", {"query": "押金", "limit": 1}, "partial"),
            f.tool("search_info_units", {"query": "不存在的独立凭证"}, "empty"), f.finish("查找材料")])
        view = self.view(calls[-1])
        self.assertIsNone(view["previous_tool_result"]["recovery"])
        self.assertEqual([n["status"] for n in view["previous_tool_result"]["notices"]], ["empty"])
        self.assertFalse(view["previous_tool_result"]["result"].get("has_more"))

    def test_failed_repeat_read_replaces_prior_has_more_notice(self):
        original = tools.search_info_units
        counter = []
        def search(case, **args):
            counter.append(args)
            return original(case, **args) if len(counter) == 1 else tools._failure(
                AppError("read_unavailable", "The query could not be completed"), status="error")
        with patch.object(tools, "search_info_units", side_effect=search):
            _, calls = self.run_script("继续核查", [f.tool("search_info_units", {"query": "押金", "limit": 1}, "partial"),
                f.tool("search_info_units", {"query": "押金", "limit": 1}, "failed"), f.finish("继续核查")])
        view = self.view(calls[2])
        self.assertEqual(view["previous_tool_result"]["recovery"]["code"], "read_unavailable")
        self.assertEqual(view["previous_tool_result"]["notices"], [])
        self.assertFalse(view["previous_tool_result"]["result"].get("has_more"))

    def test_context_trimming_removes_material_before_the_latest_repair_instruction(self):
        original = tools.get_evidence
        ref = next(u for u in self.case.info_units if u.id == "U029").source_refs[0].model_dump(mode="json")
        # Exercise projection pressure with a full-size tool payload; cached TXT
        # validation itself is covered separately, not changed by this fixture.
        ref.update(quote="甲" * 8000, start=0, end=8000)
        def read(case, **args):
            if args.get("limit") == 13:
                return original(case, **args)
            return {**tools._result([{"info_unit_id": "U029", "source_ref_index": 0, "source_ref": ref}]),
                    "total_count": 1, "next_offset": None}
        with patch.object(tools, "get_evidence", side_effect=read), patch.object(state, "MAX_CONTEXT_CHARS", 9000):
            _, calls = self.run_script("检查反馈裁剪", [f.tool("get_evidence", {"info_unit_id": "U029"}, "large"),
                f.tool("get_evidence", {"info_unit_id": "U029", "limit": 13}, "invalid"),
                f.finish("检查反馈裁剪", status="completed", basis="general")])
        view = self.view(calls[2])
        self.assertEqual(view["previous_tool_result"]["recovery"]["code"], "invalid_limit")
        self.assertTrue(view["previous_tool_result"]["recovery"]["next_action"]["steps"][0]["instruction"])
        self.assertNotIn("context_truncated", view["materials"]["source_refs"])
        self.assertNotIn("omitted_ids", view["materials"]["source_refs"])
        self.assertLessEqual(len(json.dumps(view, ensure_ascii=False)), 9000)
        self.assertEqual(len(self.audits()[0]["knowledge_state"]["source_refs"]), 1)

    def test_ambiguous_native_protocol_is_terminal_without_forcing_another_model_turn(self):
        malformed = f.tool("search_info_units", {"query": "月租"})
        malformed.message["tool_calls"].append(deepcopy(malformed.message["tool_calls"][0]))
        with self.assertRaises(AppError) as caught:
            self.run_script("检查协议", [malformed])
        self.assertEqual(caught.exception.code, "investigation_protocol_invalid")
        audit = self.audits()[0]
        self.assertEqual(len(audit["model_calls"]), 1)
        self.assertEqual(audit["tool_calls"], [])
        self.assertEqual(audit["status"], "failed")
