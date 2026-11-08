"""Offline transport-to-storage regression for whole-run Investigation commits."""
from itertools import permutations
import json
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor

import test_reasoning_proposals as fixture
import test_investigation_agent as agent_fixture
from app.case_store import case_dir, artifact_path
from app.errors import AppError
from app.issue_workspaces import get_issue_workspaces
from app.reasoning_models import PROPOSAL_ADAPTER
from app.reasoning_proposals import commit_investigation


class TestInvestigationCommit(unittest.TestCase):
    setUp = fixture.TestReasoningProposals.setUp
    tearDown = fixture.TestReasoningProposals.tearDown
    edit = fixture.TestReasoningProposals.edit
    hypothesis = fixture.TestReasoningProposals.hypothesis
    investigate = fixture.TestReasoningProposals.investigate

    def state(self):
        return get_issue_workspaces(self.case).workspaces[self.issue].reasoning

    def raw(self):
        path = case_dir(self.case.case_id) / "issue_workspaces.json"
        return path.read_bytes() if path.exists() else None

    def replay(self, result):
        audit = json.loads((case_dir(self.case.case_id) / f"runs/{result['run_id']}.output.json").read_text(encoding="utf-8"))
        return commit_investigation(self.case, self.issue, result["run_id"],
            [PROPOSAL_ADAPTER.validate_python(p) for p in result["proposals"]], audit["workspace_receipt"])

    def reverse(self, run_id, restore=False):
        return self.client.post(f"/api/cases/{self.case.case_id}/investigation-changes/reverse", json={
            "issue": self.issue, "run_id": run_id, "restore": restore})

    def retry(self, run_id):
        return self.client.post(f"/api/cases/{self.case.case_id}/investigation-changes/retry", json={
            "issue": self.issue, "run_id": run_id})

    def run_proposals(self, proposals, selected=None, **kwargs):
        response, calls = self.investigate(proposals, selected, read_ids=["U026", "U029", "U036"], **kwargs)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json(), calls

    def run_at_budget(self, *, before_finish=None, invalid_finish=False):
        proposals = [fixture.suggestion("hypothesis"),
            {**fixture.suggestion("hypothesis"), "content": "新租客没有实际支付租金"},
            {**fixture.suggestion("evidence_gap"), "hypothesis_proposal_indices": [0, 1]},
            {**fixture.suggestion("evidence_link"), "hypothesis_proposal_indices": [1]}]
        def respond(call, turn):
            if turn <= 4:
                return agent_fixture.tool("get_evidence", {"info_unit_id": ("U029", "U036", "U026", "U004")[turn - 1]}, f"read-{turn}")
            if turn <= 8:
                state = json.loads(call["messages"][1]["content"])
                return fixture.proposal_call(proposals[turn - 5],
                    [p["proposal_id"] for p in state["proposals"]], f"proposal-{turn}")
            if before_finish:
                before_finish()
            if invalid_finish:
                return agent_fixture.tool("finish_investigation", {"bad_field": turn}, f"bad-{turn}")
            return agent_fixture.finish('核对', status='budget_exhausted')
        return agent_fixture.TestInvestigationAgent.run_script(self, "核对", respond, issue_key=self.issue)

    def test_first_run_creates_parents_shared_gap_and_link_in_one_write(self):
        parents = [fixture.suggestion("hypothesis"), {**fixture.suggestion("hypothesis"), "content": "新租客没有实际支付租金"}]
        gap = {**fixture.suggestion("evidence_gap"), "hypothesis_proposal_indices": [0, 1]}
        link = {**fixture.suggestion("evidence_link"), "hypothesis_proposal_indices": [1]}
        from app import reasoning_proposals
        with patch.object(reasoning_proposals, "persist_issue_workspaces", wraps=reasoning_proposals.persist_issue_workspaces) as save:
            result, _ = self.run_proposals([*parents, gap, link])
        self.assertEqual(save.call_count, 1)
        self.assertEqual(result["workspace_commit"]["status"], "applied", result)
        state = self.state()
        self.assertEqual(len(state.hypotheses), 2)
        self.assertEqual(set(state.gaps[0].hypothesis_ids), {h.id for h in state.hypotheses})
        self.assertEqual(state.links[0].hypothesis_id, state.hypotheses[1].id)
        self.assertTrue(all(h.judgment == "unverified" for h in state.hypotheses))
        saved = self.raw()
        self.assertEqual(self.replay(result)["status"], "applied")
        self.assertEqual(self.raw(), saved)
        # The old per-item entry point is removed.
        response = self.client.post(f"/api/cases/{self.case.case_id}/reasoning-proposals/accept", json={})
        self.assertEqual(response.status_code, 404)

    def test_status_gap_and_link_commute_without_self_invalidating(self):
        for order in permutations(range(3)):
            with self.subTest(order=order):
                hid = self.hypothesis(f"独立核查命题 {order}")
                gid = self.edit(gap={"content": f"实际收租情况 {order}", "hypothesis_ids": [hid], "basis_info_unit_ids": ["U026"]}).json()["object_id"]
                status = {**fixture.suggestion("hypothesis_status", hid), "judgment": "verified"}
                update = {"kind": "evidence_gap_update", "gap_id": gid, "addressing_info_unit_ids": ["U029"],
                    "status": "open", "rationale": "仅部分回应"}
                proposals = [status, update, fixture.suggestion("evidence_link", hid)]
                result, _ = self.run_proposals([proposals[i] for i in order], [gid])
                self.assertEqual(result["workspace_commit"]["status"], "applied", result)
                target = next(h for h in self.state().hypotheses if h.id == hid)
                self.assertEqual(target.judgment, "verified")
                self.assertEqual(target.content, f"独立核查命题 {order}")
                gap = next(g for g in self.state().gaps if g.id == gid)
                self.assertEqual(gap.addressing_info_unit_ids, ["U029"])
                self.assertEqual(gap.status, "open")
                self.assertEqual(gap.basis_info_unit_ids, ["U026"])

    def test_three_status_gap_proposals_commit_with_completed_and_followup_questions(self):
        # Reproduce the A2 Trace's transport shape, not its semantic evidence quality.
        first, second = self.hypothesis("指定期间租金已支付"), self.hypothesis("指定期间租金未支付")
        gap_id = self.edit(gap={"content": "指定期间收款是否连续？", "hypothesis_ids": [first, second],
            "basis_info_unit_ids": ["U026"]}).json()["object_id"]
        saved = self.raw()
        proposals = [{**fixture.suggestion("hypothesis_status", first), "judgment": "verified"},
            {"kind": "evidence_gap_update", "gap_id": gap_id, "addressing_info_unit_ids": ["U029"],
             "status": "resolved", "rationale": "离线协议样例，模拟已有效提交的回应。"},
            {**fixture.suggestion("hypothesis_status", second), "judgment": "refuted"}]
        followups = ["后续可调查其他期间的收支，不影响本轮指定期间的完成判断。"]
        script = [agent_fixture.tool("get_evidence", {"info_unit_id": "U029"}),
            *(fixture.proposal_call(p, [], f"proposal-{i}") for i, p in enumerate(proposals))]
        def respond(call, turn):
            if turn <= len(script):
                return script[turn - 1]
            self.assertEqual(self.raw(), saved)
            return agent_fixture.finish("更新指定期间的命题判断和原缺口", status="completed", questions=followups,
                answer="已根据本轮取得的材料提交判断与缺口更新，另保留后续调查方向。")
        from app import reasoning_proposals
        with patch.object(reasoning_proposals, "persist_issue_workspaces", wraps=reasoning_proposals.persist_issue_workspaces) as save:
            result, _ = agent_fixture.TestInvestigationAgent.run_script(self,
                "更新指定期间的命题判断和原缺口", respond, issue_key=self.issue)
        self.assertEqual(result["completion_status"], "completed")
        self.assertEqual(result["open_questions"], followups)
        self.assertEqual(result["workspace_commit"]["status"], "applied", result)
        self.assertEqual(result["tool_budget_used"]["finish"], 1)
        self.assertEqual(len(result["proposals"]), 3)
        self.assertEqual(len(result["workspace_commit"]["changes"]), 3)
        self.assertEqual(save.call_count, 1)
        state = self.state()
        self.assertEqual([h.judgment for h in state.hypotheses], ["verified", "refuted"])
        self.assertEqual([h.content for h in state.hypotheses], ["指定期间租金已支付", "指定期间租金未支付"])
        self.assertEqual(state.gaps[0].id, gap_id)
        self.assertEqual(state.gaps[0].status, "resolved")
        self.assertEqual(state.gaps[0].basis_info_unit_ids, ["U026"])
        self.assertEqual(state.gaps[0].addressing_info_unit_ids, ["U029"])
        self.assertEqual(state.links, [])
        audit = json.loads((case_dir(self.case.case_id) / f"runs/{result['run_id']}.output.json").read_text(encoding="utf-8"))
        self.assertTrue(all(tool["result"]["status"] == "ok" for tool in audit["tool_calls"]))
        self.assertNotIn("unresolved_goal", json.dumps(audit))

    def test_noop_status_does_not_stage_write_or_increment_revision(self):
        hid = self.hypothesis()
        saved = self.raw()
        result, calls = self.run_proposals([fixture.suggestion("hypothesis_status", hid)], [hid])
        self.assertEqual(result["proposals"], [])
        self.assertEqual(result["workspace_commit"]["status"], "no_change")
        self.assertEqual(self.state().hypotheses[0].revision, 1)
        self.assertEqual(self.raw(), saved)
        next_input = json.loads(calls[-1]["messages"][1]["content"])
        self.assertTrue(next_input["previous_tool_result"]["recovery"]["no_change"])
        self.assertEqual(next_input["previous_tool_result"]["recovery"]["status"], "ok")

    def test_noop_does_not_invalidate_real_sibling_change(self):
        hid = self.hypothesis()
        result, _ = self.run_proposals([fixture.suggestion("hypothesis_status", hid), fixture.suggestion("evidence_link", hid)], [hid])
        self.assertEqual([p["kind"] for p in result["proposals"]], ["evidence_link"])
        self.assertEqual(self.state().hypotheses[0].revision, 2)
        self.assertEqual(result["workspace_commit"]["status"], "applied")

    def test_commit_failure_keeps_all_proposals_and_no_partial_objects(self):
        hid = self.hypothesis()
        saved = self.raw()
        with patch("app.issue_workspaces.write_json", side_effect=OSError("disk full")):
            result, _ = self.run_proposals([fixture.suggestion("hypothesis"), fixture.suggestion("evidence_link", hid)], [hid])
        self.assertEqual(result["workspace_commit"]["status"], "failed")
        self.assertEqual(len(result["proposals"]), 2)
        self.assertEqual(self.raw(), saved)
        audit_path = case_dir(self.case.case_id) / f"runs/{result['run_id']}.output.json"
        audit_before = audit_path.read_bytes()
        self.assertEqual(self.retry(result["run_id"]).json()["workspace_commit"]["status"], "applied")
        self.assertEqual(audit_path.read_bytes(), audit_before)
        saved = self.raw()
        self.assertEqual(self.retry(result["run_id"]).json()["workspace_commit"]["status"], "applied")
        self.assertEqual(self.raw(), saved)
        self.assertEqual(len(self.state().hypotheses), 2)
        self.assertEqual(len(self.state().links), 1)

    def test_user_edit_during_run_blocks_whole_commit_and_preserves_edit(self):
        hid = self.hypothesis()
        def user_edit():
            self.assertEqual(self.edit(hypothesis={"id": hid, "content": "用户已修正命题"}).status_code, 200)
        result, _ = self.run_proposals([fixture.suggestion("hypothesis"), fixture.suggestion("evidence_link", hid)], [hid], before_finish=user_edit)
        self.assertEqual(result["workspace_commit"]["status"], "conflict")
        self.assertEqual(len(self.state().hypotheses), 1)
        self.assertEqual(self.state().hypotheses[0].content, "用户已修正命题")
        self.assertEqual(self.state().links, [])
        with self.assertRaises(AppError):
            self.replay(result)

    def test_finish_preserves_user_issue_switch_during_investigation(self):
        from app.issue_workspaces import update_issue_workspace, IssueWorkspacePatch
        hid = self.hypothesis()
        other = self.case.core_issue
        result, _ = self.run_proposals([fixture.suggestion("evidence_link", hid)], [hid],
            before_finish=lambda: update_issue_workspace(self.case, IssueWorkspacePatch(issue=other)))
        self.assertEqual(result["workspace_commit"]["status"], "applied")
        self.assertEqual(get_issue_workspaces(self.case).active_issue, other)
        self.assertEqual(len(self.state().links), 1)

    def test_evidence_edit_during_run_blocks_whole_commit(self):
        hid = self.hypothesis()
        saved = self.raw()
        unit = next(u for u in self.case.info_units if u.id == "U029")
        original = unit.content
        try:
            result, _ = self.run_proposals([fixture.suggestion("hypothesis"), fixture.suggestion("evidence_link", hid)], [hid],
                before_finish=lambda: setattr(unit, "content", "用户修正材料"))
            self.assertEqual(result["workspace_commit"]["status"], "conflict")
            self.assertEqual(self.raw(), saved)
        finally:
            unit.content = original

    def test_cached_source_changed_before_finish_rejects_whole_batch(self):
        hid = self.hypothesis()
        saved = self.raw()
        ref = next(u for u in self.case.info_units if u.id == "U029").source_refs[0]
        file = next(f for f in self.case.files if f.file_id == ref.file_id)
        path = artifact_path(self.case.case_id, file.parsed_text_path)
        original = path.read_bytes()
        try:
            result, _ = self.run_proposals([fixture.suggestion("hypothesis"), fixture.suggestion("evidence_link", hid)], [hid],
                before_finish=lambda: path.write_bytes(b"changed source text"))
            self.assertEqual(result["workspace_commit"]["status"], "conflict")
            self.assertEqual(self.raw(), saved)
        finally:
            path.write_bytes(original)

    def test_final_audit_failure_preserves_authoritative_atomic_run_record(self):
        from app import investigation_agent as agent
        original_write = agent._write_audit
        def fail_final(case_id, path, payload):
            if payload.get("result", {}).get("workspace_commit", {}).get("status") == "applied":
                raise AppError("investigation_audit_failed", "final audit failed")
            return original_write(case_id, path, payload)
        with patch.object(agent, "_write_audit", side_effect=fail_final):
            result, _ = self.run_proposals([fixture.suggestion("hypothesis")])
        self.assertEqual(result["workspace_commit"]["status"], "applied")
        self.assertEqual(self.state().investigation_runs[result["run_id"]].status, "applied")
        self.assertTrue(any("审计" in message for message in result["limitations"]))
        saved = self.raw()
        self.assertEqual(self.retry(result["run_id"]).status_code, 200)
        self.assertEqual(self.raw(), saved)

    def test_model_failure_never_commits_but_keeps_proposals(self):
        hid = self.hypothesis()
        saved = self.raw()
        result, _ = self.run_proposals([fixture.suggestion("evidence_link", hid)], [hid], summary_failure=True)
        self.assertEqual(result["summary_status"], "failed")
        self.assertEqual(result["workspace_commit"]["status"], "discarded")
        self.assertEqual(len(result["proposals"]), 1)
        self.assertEqual(self.raw(), saved)
        self.assertEqual(self.retry(result["run_id"]).status_code, 409)
        self.assertEqual(self.raw(), saved)

    def test_eight_tools_then_valid_finish_atomically_commits_all_proposals(self):
        from app import reasoning_proposals
        saved = self.raw()
        with patch.object(reasoning_proposals, "persist_issue_workspaces", wraps=reasoning_proposals.persist_issue_workspaces) as save:
            result, _ = self.run_at_budget(before_finish=lambda: self.assertEqual(self.raw(), saved))
        self.assertEqual(save.call_count, 1)
        self.assertEqual(result["tool_budget_used"], {"read": 4, "proposal": 4, "finish": 1})
        self.assertEqual(result["summary_status"], "completed")
        self.assertEqual(result["completion_status"], "budget_exhausted")
        self.assertEqual(result["workspace_commit"]["status"], "applied")
        self.assertEqual(len(result["workspace_commit"]["changes"]), 4)
        self.assertNotIn("findings", result)
        state = self.state()
        self.assertEqual(len(state.hypotheses), 2)
        self.assertEqual(set(state.gaps[0].hypothesis_ids), {h.id for h in state.hypotheses})
        self.assertEqual(state.links[0].hypothesis_id, state.hypotheses[1].id)
        self.assertEqual(state.investigation_runs[result["run_id"]].model_dump(mode="json"), result["workspace_commit"])

    def test_eight_tools_then_storage_failure_can_retry_once_without_duplicates(self):
        saved = self.raw()
        with patch("app.issue_workspaces.write_json", side_effect=OSError("disk full")):
            result, _ = self.run_at_budget()
        self.assertEqual(result["completion_status"], "budget_exhausted")
        self.assertEqual(result["workspace_commit"]["status"], "failed")
        self.assertEqual(len(result["proposals"]), 4)
        self.assertEqual(self.raw(), saved)
        audit_path = case_dir(self.case.case_id) / f"runs/{result['run_id']}.output.json"
        audit_before = audit_path.read_bytes()
        from app import reasoning_proposals
        with patch.object(reasoning_proposals, "persist_issue_workspaces", wraps=reasoning_proposals.persist_issue_workspaces) as save:
            first = self.retry(result["run_id"])
            self.assertEqual(first.status_code, 200, first.text)
            saved = self.raw()
            second = self.retry(result["run_id"])
        self.assertEqual(save.call_count, 1)
        self.assertEqual(first.json()["workspace_commit"]["status"], "applied")
        self.assertEqual(second.json()["workspace_commit"], first.json()["workspace_commit"])
        self.assertEqual(audit_path.read_bytes(), audit_before)
        self.assertEqual(self.raw(), saved)
        self.assertEqual(len(self.state().hypotheses), 2)
        self.assertEqual(len(self.state().gaps), 1)
        self.assertEqual(len(self.state().links), 1)

    def test_eight_tools_without_valid_finish_never_commits_or_allows_retry(self):
        saved = self.raw()
        result, _ = self.run_at_budget(invalid_finish=True)
        self.assertEqual(result["summary_status"], "failed")
        self.assertEqual(result["completion_status"], "budget_exhausted")
        self.assertEqual(result["workspace_commit"]["status"], "discarded")
        self.assertEqual(len(result["proposals"]), 4)
        self.assertEqual(self.retry(result["run_id"]).status_code, 409)
        self.assertEqual(self.raw(), saved)

    def test_eight_tools_then_concurrent_edit_blocks_commit_and_retry(self):
        hid = self.hypothesis()
        def user_edit():
            self.assertEqual(self.edit(hypothesis={"id": hid, "content": "用户已修正命题"}).status_code, 200)
        result, _ = self.run_at_budget(before_finish=user_edit)
        self.assertEqual(result["summary_status"], "failed")
        self.assertEqual(result["finish_status"], "validated")
        self.assertEqual(result["completion_status"], "budget_exhausted")
        self.assertEqual(result["workspace_commit"]["status"], "conflict")
        self.assertEqual(len(self.state().hypotheses), 1)
        self.assertEqual(self.state().hypotheses[0].content, "用户已修正命题")
        self.assertEqual(self.state().gaps, [])
        self.assertEqual(self.state().links, [])
        saved = self.raw()
        self.assertEqual(self.retry(result["run_id"]).status_code, 409)
        self.assertEqual(self.raw(), saved)

    def test_proposals_do_not_write_before_finish_and_concurrent_retries_apply_once(self):
        hid = self.hypothesis()
        saved = self.raw()
        from app import reasoning_proposals
        # Save fails only after the pre-Finish assertion has observed the original workspace.
        with patch.object(reasoning_proposals, "persist_issue_workspaces", side_effect=OSError("disk full")):
            result, _ = self.run_proposals([fixture.suggestion("hypothesis"), fixture.suggestion("evidence_link", hid)], [hid],
                before_finish=lambda: self.assertEqual(self.raw(), saved))
        self.assertEqual(self.raw(), saved)
        with patch.object(reasoning_proposals, "persist_issue_workspaces", wraps=reasoning_proposals.persist_issue_workspaces) as save:
            with ThreadPoolExecutor(max_workers=2) as pool:
                replies = list(pool.map(lambda _: self.replay(result), range(2)))
        self.assertEqual(save.call_count, 1)
        self.assertTrue(all(reply["status"] == "applied" for reply in replies))
        self.assertEqual(len(self.state().hypotheses), 2)
        self.assertEqual(len(self.state().links), 1)

    def test_identical_link_is_noop_and_changed_meaning_updates_stable_id(self):
        hid = self.hypothesis()
        proposal = fixture.suggestion("evidence_link", hid)
        first, _ = self.run_proposals([proposal], [hid])
        self.assertEqual(first["workspace_commit"]["status"], "applied")
        link = self.state().links[0]
        saved = self.raw()
        second, _ = self.run_proposals([proposal], [hid])
        self.assertEqual(second["workspace_commit"]["status"], "no_change")
        self.assertEqual(second["proposals"], [])
        self.assertEqual(self.raw(), saved)
        third, _ = self.run_proposals([{**proposal, "meaning": "support"}], [hid])
        self.assertEqual(third["workspace_commit"]["status"], "applied")
        self.assertEqual(len(self.state().links), 1)
        self.assertEqual(self.state().links[0].id, link.id)
        self.assertEqual(self.state().links[0].meaning, "support")

    def test_undo_restore_and_replay_are_idempotent_and_keep_ids(self):
        result, _ = self.run_proposals([fixture.suggestion("hypothesis"),
            {**fixture.suggestion("evidence_gap"), "hypothesis_proposal_indices": [0]}])
        original = self.state().model_copy(deep=True)
        run = result["run_id"]
        self.assertEqual(self.reverse(run).status_code, 200)
        self.assertEqual(self.state().hypotheses, [])
        saved = self.raw()
        self.assertEqual(self.reverse(run).status_code, 200)
        self.assertEqual(self.replay(result)["status"], "undone")
        self.assertEqual(self.raw(), saved)
        self.assertEqual(self.reverse(run, True).status_code, 200)
        self.assertEqual(self.state(), original)
        target = self.state().hypotheses[0]
        self.edit(hypothesis={"id": target.id, "content": "用户改写了命题"})
        saved = self.raw()
        self.assertEqual(self.reverse(run).status_code, 409)
        self.assertEqual(self.replay(result)["status"], "applied")
        self.assertEqual(self.raw(), saved)

    def test_undo_refuses_later_dependent_objects(self):
        result, _ = self.run_proposals([fixture.suggestion("hypothesis")])
        hid = self.state().hypotheses[0].id
        self.edit(gap={"content": "用户新增关联问题", "hypothesis_ids": [hid]})
        saved = self.raw()
        self.assertEqual(self.reverse(result["run_id"]).status_code, 409)
        self.assertEqual(self.raw(), saved)

    def test_deleted_agent_object_is_not_recreated_by_repeat_commit(self):
        result, _ = self.run_proposals([fixture.suggestion("hypothesis")])
        hid = self.state().hypotheses[0].id
        self.edit(delete_hypothesis_id=hid)
        saved = self.raw()
        self.replay(result)
        self.assertEqual(self.raw(), saved)
        self.assertEqual(self.state().hypotheses, [])
