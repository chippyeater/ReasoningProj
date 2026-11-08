"""Run isolation, candidate edits and direct snapshot commits; no real model calls."""
import asyncio
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

import test_investigation_agent as f
from app import investigation_actions as actions, investigation_agent as agent, llm_client, reasoning_proposals
from app.case_store import case_dir
from app.errors import AppError
from app.investigation_state import RunWorkspace
from app.issue_workspaces import get_issue_workspaces, investigation_context
from app.reasoning_models import ReasoningEdit
from app.reasoning_service import edit_reasoning


class TestInvestigationWorkingWorkspace(unittest.TestCase):
    run_script = f.TestInvestigationAgent.run_script
    audits = f.TestInvestigationAgent.audits
    tearDown = f.TestInvestigationAgent.tearDown

    def setUp(self):
        f.TestInvestigationAgent.setUp(self)
        self._workspace_enabled = True
        self.issue = self.case.issue_set[1]
        _, self.hid = edit_reasoning(self.case, ReasoningEdit.model_validate({
            "issue": self.issue, "hypothesis": {"content": "指定期间租金已支付"}}))
        _, self.gid = edit_reasoning(self.case, ReasoningEdit.model_validate({
            "issue": self.issue, "gap": {"content": "收款是否连续？", "hypothesis_ids": [self.hid]}}))
        self.context = investigation_context(self.case, self.issue, selected_reasoning_ids=[self.gid])
        self.working = RunWorkspace(get_issue_workspaces(self.case), self.issue, "investigation-" + "a" * 32)
        self.path = case_dir(self.case.case_id) / "issue_workspaces.json"
        self.saved = self.path.read_bytes()

    def status(self, judgment, call_id):
        return f.tool("propose_hypothesis_status", {"hypothesis_id": self.hid, "judgment": judgment,
            "info_unit_ids": ["U029"], "rationale": "本轮已取得材料，按实际证明力修改判断。"}, call_id)

    def apply_status(self, workspace, proposals, judgment):
        return actions.propose_hypothesis_status(self.case,
            {"units": {"U029"}, "relations": set(), "evidence": set()}, workspace, proposals,
            hypothesis_id=self.hid, judgment=judgment, info_unit_ids=["U029"],
            rationale="记录本次操作的材料依据。")

    def test_latest_objects_are_visible_and_repeated_transitions_are_not_blocked(self):
        expected = ["unverified", "verified", "refuted", "verified", "verified"]
        script = [self.status("verified", "v1"), self.status("refuted", "r1"),
                  self.status("verified", "v2"), self.status("verified", "noop"),
                  f.finish("核查", status="completed")]
        def respond(call, turn):
            view = json.loads(call["messages"][1]["content"])
            self.assertNotIn("effective_reasoning", view)
            self.assertEqual(view["reasoning"]["hypotheses"][0]["judgment"], expected[turn - 1])
            self.assertEqual(self.path.read_bytes(), self.saved)
            return script[turn - 1]
        result, _ = self.run_script("核查", respond, selected=["U029"], issue_key=self.issue, work_context=self.context)
        self.assertEqual(result["workspace_commit"]["status"], "applied")
        self.assertEqual(len(result["proposals"]), 3)
        self.assertEqual(result["run_status"], "completed")
        audit = self.audits()[0]
        final = get_issue_workspaces(self.case).workspaces[self.issue].reasoning
        self.assertEqual(final.hypotheses[0].model_dump(mode="json"),
                         audit["workspace_receipt"]["workspace"]["reasoning"]["hypotheses"][0])
        changes = [p["changes"][0] for p in result["proposals"]]
        self.assertEqual([c["before"]["judgment"] for c in changes], expected[:3])
        self.assertEqual([c["after"]["judgment"] for c in changes], expected[1:4])
        self.assertEqual(changes[0]["after"], changes[1]["before"])
        self.assertEqual(changes[1]["after"], changes[2]["before"])

    def test_gap_search_cache_reads_current_workspace_after_each_change(self):
        update = {"gap_id": self.gid, "status": "resolved", "addressing_info_unit_ids": ["U029"], "rationale": "回应材料。"}
        search = {"query": "收款", "info_unit_ids": None, "limit": 1, "offset": 0}
        result, calls = self.run_script("回应 Gap", [f.tool("search_evidence_gaps", search, "read1"),
            f.tool("propose_evidence_gap_update", update, "update1"),
            f.tool("search_evidence_gaps", search, "read2"),
            f.tool("propose_evidence_gap_update", {**update, "status": "open", "addressing_info_unit_ids": ["U036"]}, "update2"),
            f.tool("search_evidence_gaps", search, "read3"), f.finish("回应 Gap", status="completed")],
            selected=["U029", "U036"], issue_key=self.issue, work_context=self.context)
        for index, status, ids in [(1, "open", []), (3, "resolved", ["U029"]), (5, "open", ["U029", "U036"])]:
            view = json.loads(calls[index]["messages"][1]["content"])
            gap = view["previous_tool_result"]["result"]["items"][0]["gaps"][0]
            self.assertEqual((gap["status"], gap["addressing_info_unit_ids"]), (status, ids))
        self.assertEqual(len(result["proposals"]), 2)
        self.assertEqual(get_issue_workspaces(self.case).workspaces[self.issue].reasoning.hypotheses[0].judgment, "unverified")

    def test_candidate_failure_after_edit_leaves_working_and_shared_workspace_unchanged(self):
        original = reasoning_proposals.edit_reasoning
        def fail(*args, **kwargs):
            original(*args, **kwargs)
            raise AppError("candidate_rejected", "Reject after mutating candidate")
        before = self.working.current.model_dump_json()
        proposals = []
        with patch.object(reasoning_proposals, "edit_reasoning", side_effect=fail), self.assertRaises(AppError):
            self.apply_status(self.working, proposals, "verified")
        self.assertEqual(self.working.current.model_dump_json(), before)
        self.assertEqual(proposals, [])
        self.assertEqual(self.path.read_bytes(), self.saved)

    def test_tool_failure_after_candidate_application_is_not_published(self):
        original = actions.PROPOSAL_TOOLS["propose_hypothesis_status"]
        def fail(*args, **kwargs):
            original(*args, **kwargs)
            raise AppError("feedback_failed", "Reject entire tool execution")
        with patch.dict(actions.PROPOSAL_TOOLS, {"propose_hypothesis_status": fail}):
            result, calls = self.run_script("核查", [self.status("verified", "update"),
                f.finish("核查", status="completed")], selected=["U029"], issue_key=self.issue, work_context=self.context)
        self.assertEqual(json.loads(calls[1]["messages"][1]["content"])["reasoning"]["hypotheses"][0]["judgment"], "unverified")
        self.assertEqual(result["proposals"], [])
        self.assertEqual(self.path.read_bytes(), self.saved)

    def test_finish_commits_the_final_snapshot_without_executing_edits(self):
        def respond(call, turn):
            if turn == 1:
                return self.status("verified", "update")
            self.commit_guard = patch.object(reasoning_proposals, "edit_reasoning", side_effect=AssertionError("Commit replayed edits"))
            self.commit_guard.start()
            self.addCleanup(self.commit_guard.stop)
            return f.finish("核查", status="completed")
        result, _ = self.run_script("核查", respond, selected=["U029"], issue_key=self.issue, work_context=self.context)
        self.assertEqual(result["workspace_commit"]["status"], "applied")

    def test_cancel_after_a_successful_edit_discards_the_run_and_cannot_retry(self):
        async def exercise():
            waiting = asyncio.Event()
            count = 0
            async def respond(*args, **kwargs):
                nonlocal count
                count += 1
                if count == 1:
                    return self.status("verified", "update")
                waiting.set()
                await asyncio.Event().wait()
            with patch.object(llm_client, "call_tools", side_effect=respond):
                task = asyncio.create_task(agent.run_investigation(self.case, "核查", ["U029"],
                    issue_key=self.issue, work_context=self.context))
                await waiting.wait()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        asyncio.run(exercise())
        self.assertEqual(self.path.read_bytes(), self.saved)
        audit = self.audits()[0]
        self.assertEqual(audit["status"], "cancelled")
        self.assertEqual(audit["result"]["workspace_commit"]["status"], "discarded")
        with self.assertRaises(AppError):
            reasoning_proposals.retry_investigation(self.case, self.issue, audit["run_id"])

    def test_concurrent_runs_are_isolated_and_only_one_can_commit(self):
        async def exercise():
            ready = {name: asyncio.Event() for name in ("A", "B")}
            first_committed = asyncio.Event()
            counts = {name: 0 for name in ready}
            async def respond(config, messages, *args, **kwargs):
                view = json.loads(messages[1]["content"])
                name = view["goal"]
                counts[name] += 1
                judgment = "verified" if name == "A" else "refuted"
                if counts[name] == 1:
                    self.assertEqual(view["reasoning"]["hypotheses"][0]["judgment"], "unverified")
                    ready[name].set()
                    await ready["B" if name == "A" else "A"].wait()
                    return self.status(judgment, "update")
                self.assertEqual(view["reasoning"]["hypotheses"][0]["judgment"], judgment)
                if name == "B":
                    await first_committed.wait()
                return f.finish(name, status="completed")
            async def run(name):
                result = await agent.run_investigation(self.case, name, ["U029"], issue_key=self.issue, work_context=self.context)
                if name == "A":
                    first_committed.set()
                return result
            with patch.object(llm_client, "call_tools", side_effect=respond):
                return await asyncio.gather(run("A"), run("B"))
        first, second = asyncio.run(exercise())
        self.assertEqual(first["workspace_commit"]["status"], "applied")
        self.assertEqual(second["workspace_commit"]["status"], "conflict")
        self.assertEqual((second["finish_status"], second["run_status"], second["summary_status"]), ("validated", "failed", "failed"))
        self.assertNotEqual(second["completion_status"], "completed")
        self.assertIsNone(second["answer"])
        self.assertEqual(get_issue_workspaces(self.case).workspaces[self.issue].reasoning.hypotheses[0].judgment, "verified")

    def test_disconnect_after_finish_validation_still_cancels_before_commit(self):
        count = 0
        async def respond(*args, **kwargs):
            nonlocal count
            count += 1
            return self.status("verified", "update") if count == 1 else f.finish("核查", status="completed")
        async def disconnected():
            return any(audit["status"] == "committing" for audit in self.audits())
        with patch.object(llm_client, "call_tools", side_effect=respond), self.assertRaises(asyncio.CancelledError):
            asyncio.run(agent.run_investigation(self.case, "核查", ["U029"], issue_key=self.issue,
                work_context=self.context, is_cancelled=disconnected))
        self.assertEqual(self.path.read_bytes(), self.saved)
        audit = self.audits()[0]
        self.assertEqual(audit["status"], "cancelled")
        self.assertEqual(audit["result"]["finish_status"], "failed")
        self.assertEqual(audit["result"]["workspace_commit"]["status"], "discarded")

    def test_audit_must_match_final_snapshot_and_failed_finish_never_commits(self):
        proposals = []
        self.apply_status(self.working, proposals, "verified")
        receipt = reasoning_proposals.workspace_receipt(self.case, self.working, proposals)
        tampered = deepcopy(receipt)
        tampered["workspace"]["reasoning"]["hypotheses"][0]["judgment"] = "refuted"
        with self.assertRaises(AppError):
            reasoning_proposals.commit_investigation(self.case, self.issue, self.working.run_id, proposals, tampered)
        self.assertEqual(self.path.read_bytes(), self.saved)
        result, _ = self.run_script("核查", [self.status("verified", "update"),
            *(f.tool("finish_investigation", {"invalid": True}, f"bad-{i}") for i in range(3))],
            selected=["U029"], issue_key=self.issue, work_context=self.context)
        self.assertEqual(result["run_status"], "failed")
        self.assertEqual(result["workspace_commit"]["status"], "discarded")
        self.assertEqual(self.path.read_bytes(), self.saved)

    def test_removed_overlay_and_batch_replay_have_no_runtime_entrypoint(self):
        from app import investigation_state
        self.assertFalse(hasattr(investigation_state, "effective_reasoning"))
        self.assertFalse(hasattr(reasoning_proposals, "stage_proposals"))

    def test_new_hypothesis_can_be_updated_and_linked_using_its_actual_working_id(self):
        ids = []
        def respond(call, turn):
            view = json.loads(call["messages"][1]["content"])
            if turn == 1:
                return f.tool("propose_hypothesis", {"content": "替租人实际付款", "rationale": "需核对实际履行。",
                    "info_unit_ids": ["U029"]}, "create")
            if turn == 2:
                hid = view["previous_tool_result"]["result"]["object_id"]
                ids.append(hid)
                self.assertIn(hid, {h["id"] for h in view["reasoning"]["hypotheses"]})
                return f.tool("propose_hypothesis_status", {"hypothesis_id": hid, "judgment": "refuted",
                    "rationale": "依据本轮材料修改判断。", "info_unit_ids": ["U029"]}, "update")
            if turn == 3:
                return f.tool("propose_evidence_gap", {"content": "实际付款对象？", "rationale": "独立核查条件。",
                    "basis_info_unit_ids": ["U029"], "hypothesis_targets": {
                        "hypothesis_ids": ids, "hypothesis_proposal_ids": []}}, "gap")
            return f.finish("核查", status="completed")
        result, _ = self.run_script("核查", respond, selected=["U029"], issue_key=self.issue, work_context=self.context)
        state = get_issue_workspaces(self.case).workspaces[self.issue].reasoning
        self.assertEqual(result["workspace_commit"]["status"], "applied")
        self.assertEqual(next(h.judgment for h in state.hypotheses if h.id == ids[0]), "refuted")
        self.assertEqual(state.gaps[-1].hypothesis_ids, ids)

    def test_direct_commit_preserves_live_canvas_and_other_issue_changes(self):
        from app.issue_workspaces import persist_issue_workspaces
        proposals = []
        self.apply_status(self.working, proposals, "verified")
        receipt = reasoning_proposals.workspace_receipt(self.case, self.working, proposals)
        live = get_issue_workspaces(self.case)
        live.workspaces[self.issue].canvas.visible_node_ids.remove(self.gid)
        live.workspaces[self.issue].canvas.node_positions[self.hid].x = 1234
        other = next(issue for issue in live.workspaces if issue != self.issue)
        live.workspaces[other].status = "closed"
        live.active_issue = other
        persist_issue_workspaces(self.case, live)
        reasoning_proposals.commit_investigation(self.case, self.issue, self.working.run_id, proposals, receipt)
        final = get_issue_workspaces(self.case)
        self.assertNotIn(self.gid, final.workspaces[self.issue].canvas.visible_node_ids)
        self.assertEqual(final.workspaces[self.issue].canvas.node_positions[self.hid].x, 1234)
        self.assertEqual(final.workspaces[other], live.workspaces[other])
        self.assertEqual(final.active_issue, other)

    def test_undo_after_repeated_changes_restores_baseline_objects(self):
        proposals = []
        self.apply_status(self.working, proposals, "verified")
        self.apply_status(self.working, proposals, "refuted")
        receipt = reasoning_proposals.workspace_receipt(self.case, self.working, proposals)
        record = reasoning_proposals.commit_investigation(self.case, self.issue, self.working.run_id, proposals, receipt)
        self.assertEqual(len(record["proposals"]), 2)
        self.assertEqual(len(record["changes"]), 1)
        reasoning_proposals.reverse_investigation(self.case, self.issue, self.working.run_id)
        state = get_issue_workspaces(self.case).workspaces[self.issue].reasoning
        self.assertEqual(state.hypotheses, self.working.baseline.hypotheses)
        self.assertEqual(state.gaps, self.working.baseline.gaps)
        self.assertEqual(state.investigation_runs[self.working.run_id].status, "undone")
