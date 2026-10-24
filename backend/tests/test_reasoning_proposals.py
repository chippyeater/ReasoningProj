"""Real HTTP/model payload boundary with mock completions, user-only adoption."""
import json
from unittest.mock import patch
import httpx

import test_investigation_agent as replies
from test_reasoning_workspace import TestReasoningWorkspace
from app import llm_client
from app.case_store import case_dir
from app.issue_workspaces import get_issue_workspaces, investigation_context


def suggestion(kind, hid=None):
    return {"kind": kind, "content": "房东是否存在可核实的空置损失？" if kind in {"hypothesis", "evidence_gap"} else None,
            "hypothesis_ids": [hid] if hid else [], "info_unit_ids": ["U029"],
            "meaning": "oppose" if kind == "evidence_link" else None,
            "judgment": "unverified" if kind == "hypothesis_status" else None,
            "rationale": "张某陈述无损失，仍应核对入住和收租材料。",
            "evidence_refs": [{"info_unit_id": "U029", "source_ref_index": 0}]}


def proposal_call(source, prior_ids, call_id):
    """Offline scenario -> native call, using only IDs returned by previous tools."""
    kind = source["kind"]
    args = {key: source[key] for key in ("rationale", "evidence_refs")}
    if kind == "evidence_link":
        args.update(hypothesis_id=source["hypothesis_ids"][0] if source["hypothesis_ids"] else None,
                    info_unit_id=source["info_unit_ids"][0], meaning=source["meaning"],
                    hypothesis_proposal_ids=source.get("hypothesis_proposal_ids", [
                        prior_ids[index] if index < len(prior_ids) else "proposal-" + "f" * 32
                        for index in source.get("hypothesis_proposal_indices", [])]))
    else:
        args["info_unit_ids"] = source["info_unit_ids"]
        if kind in {"hypothesis", "evidence_gap"}:
            args["content"] = source["content"]
        if kind == "hypothesis_status":
            args.update(hypothesis_id=source["hypothesis_ids"][0], judgment=source["judgment"])
        if kind == "evidence_gap":
            args.update(hypothesis_ids=source["hypothesis_ids"], hypothesis_proposal_ids=source.get("hypothesis_proposal_ids", [
                prior_ids[index] if index < len(prior_ids) else "proposal-" + "f" * 32
                for index in source.get("hypothesis_proposal_indices", [])]))
    return replies.tool("propose_" + kind, args, call_id)


class TestReasoningProposals(TestReasoningWorkspace):
    # Do not repeat parent tests in this suite.
    test_edit_link_gap_and_restore_without_business_copies = None
    test_invalid_refs_duplicates_and_write_failure_leave_file_unchanged = None
    test_legacy_workspace_and_referenced_evidence_are_protected = None

    def investigate(self, proposals, selected=None, *, summary_failure=False, read_ids=None):
        goal = "检查假设的支持、反对依据及证据缺口"
        requests = []
        real_client = httpx.AsyncClient
        def model(request):
            body = json.loads(request.content)
            requests.append(body)
            step = len(requests) - 1
            if step == 0:
                reply = replies.tool("get_evidence", {"info_unit_ids": read_ids or ["U029"]})
            elif step <= len(proposals or []):
                returned = [json.loads(m["content"]) for m in body["messages"] if m["role"] == "tool"]
                ids = [r["proposal_id"] for r in returned if r.get("pending")]
                reply = proposal_call(proposals[step - 1], ids, f"proposal-{step}")
            elif step == len(proposals or []) + 1:
                reply = replies.present(goal, ["U029"], refs=[("U029", 0)], status="completed")
            elif summary_failure:
                return httpx.Response(500, text="mock final model failure")
            else:
                reply = replies.final("已核对相关陈述，尚需独立材料；建议均待用户确认。")
            return httpx.Response(200, json=reply.response)
        with patch.object(llm_client.httpx, "AsyncClient", side_effect=lambda **kwargs:
                          real_client(transport=httpx.MockTransport(model), **kwargs)):
            response = self.client.post(f"/api/cases/{self.case.case_id}/agent/run", json={
                "goal": goal, "current_issue": self.issue, "selected_reasoning_ids": selected or []})
        return response, requests

    def accept(self, run_id, index=0, issue=None):
        path = case_dir(self.case.case_id) / f"runs/{run_id}.output.json"
        proposals = json.loads(path.read_text(encoding="utf-8"))["result"]["proposals"]
        reference = {"proposal_id": proposals[index]["proposal_id"]} if index < len(proposals) and proposals[index].get("proposal_id") else {"proposal_index": index}
        return self.client.post(f"/api/cases/{self.case.case_id}/reasoning-proposals/accept", json={
            "issue": issue or self.issue, "run_id": run_id, **reference, "position": {"x": 1500, "y": 500}})

    def test_validated_proposals_remain_preview_until_atomic_idempotent_adoption(self):
        hid = self.hypothesis()
        path = case_dir(self.case.case_id) / "issue_workspaces.json"
        before = path.read_bytes()
        response, requests = self.investigate([suggestion("hypothesis"), suggestion("evidence_link", hid),
                                                suggestion("evidence_gap", hid), suggestion("hypothesis_status", hid)], [hid])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(path.read_bytes(), before)
        initial = json.loads(requests[0]["messages"][1]["content"])
        self.assertEqual(initial["work_context"]["reasoning"]["hypotheses"][0]["id"], hid)
        self.assertNotIn("source_refs", json.dumps(initial))
        self.assertNotIn("node_positions", json.dumps(initial))
        self.assertNotIn("visible_node_ids", json.dumps(initial))
        self.assertNotIn("response_format", requests[0])
        run_id = response.json()["run_id"]
        self.assertEqual(self.accept(run_id, issue=self.case.core_issue).status_code, 409)
        accepted = self.accept(run_id)
        self.assertEqual(accepted.status_code, 200, accepted.text)
        obj = accepted.json()["object_id"]
        again = self.accept(run_id)
        self.assertTrue(again.json()["already_accepted"])
        self.assertEqual(again.json()["object_id"], obj)
        self.assertEqual(len(get_issue_workspaces(self.case).workspaces[self.issue].reasoning.hypotheses), 2)
        # Link adoption is independent of new Hypothesis adoption; source remains shared.
        linked = self.accept(run_id, 1)
        self.assertEqual(linked.status_code, 200, linked.text)
        link = get_issue_workspaces(self.case).workspaces[self.issue].reasoning.links[0]
        self.assertEqual((link.origin, link.meaning, link.info_unit_id), ("agent", "oppose", "U029"))
        # Later proposals based on the old target must not overwrite subsequent edits.
        self.assertEqual(self.accept(run_id, 3).status_code, 409)

    def test_user_corrections_and_evidence_changes_invalidate_old_suggestions(self):
        hid = self.hypothesis()
        response, _ = self.investigate([suggestion("hypothesis_status", hid)], [hid])
        self.assertEqual(response.status_code, 200, response.text)
        self.edit(hypothesis={"id": hid, "content": "用户修正：不能将陈述当作核实", "judgment": "refuted", "work_status": "deprecated"})
        self.assertEqual(self.accept(response.json()["run_id"]).status_code, 409)
        context = investigation_context(self.case, self.issue, "检查当前假设", [hid])
        current = context["reasoning"]["hypotheses"][0]
        self.assertEqual((current["judgment"], current["work_status"], current["last_edited_by"]), ("refuted", "deprecated", "user"))
        self.assertIn("用户修正", current["content"])
        next_run, requests = self.investigate(None, [hid])
        self.assertEqual(next_run.status_code, 200, next_run.text)
        wire = json.loads(requests[0]["messages"][1]["content"])["work_context"]["reasoning"]["hypotheses"][0]
        self.assertEqual((wire["judgment"], wire["work_status"], wire["revision"]),
                         (current["judgment"], current["work_status"], current["revision"]))
        self.assertEqual(wire["content"], current["content"])
        fresh, _ = self.investigate([suggestion("hypothesis_status", hid)], [hid])
        self.assertEqual(fresh.status_code, 200, fresh.text)
        self.assertEqual(self.accept(fresh.json()["run_id"]).status_code, 200)
        adopted = get_issue_workspaces(self.case).workspaces[self.issue].reasoning.hypotheses[0]
        self.assertEqual((adopted.judgment, adopted.work_status, adopted.content),
                         ("unverified", "deprecated", current["content"]))
        response, _ = self.investigate([suggestion("hypothesis")])
        unit = next(u for u in self.case.info_units if u.id == "U029")
        old = unit.content
        unit.content = "原材料的描述已由用户修正，需要重新调查。"
        self.assertEqual(self.accept(response.json()["run_id"]).status_code, 409)
        unit.content = old

    def test_invalid_unretrieved_evidence_unknown_targets_and_missing_suggestions(self):
        hid = self.hypothesis()
        for bad in ("unretrieved", "unknown_target", "bad_ref"):
            proposal = suggestion("evidence_link", hid)
            if bad == "unretrieved":
                proposal["info_unit_ids"] = ["U026"]
            elif bad == "unknown_target":
                proposal["hypothesis_ids"] = ["foreign"]
            else:
                proposal["evidence_refs"][0]["source_ref_index"] = 9
            response, _ = self.investigate([proposal])
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["proposals"], [])
        response, _ = self.investigate(None)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["proposals"], [])
        self.assertEqual(self.accept(response.json()["run_id"]).status_code, 409)

    def test_save_failure_has_no_partial_adoption_and_can_retry(self):
        hid = self.hypothesis()
        response, _ = self.investigate([suggestion("evidence_gap", hid)])
        self.assertEqual(response.status_code, 200, response.text)
        path = case_dir(self.case.case_id) / "issue_workspaces.json"
        before = path.read_bytes()
        with patch("app.issue_workspaces.write_json", side_effect=OSError("full")):
            self.assertEqual(self.accept(response.json()["run_id"]).status_code, 500)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.accept(response.json()["run_id"]).status_code, 200)
        state = get_issue_workspaces(self.case).workspaces[self.issue].reasoning
        self.assertEqual(len(state.gaps), 1)
        self.assertEqual(state.gaps[0].status, "open")

    def test_context_is_bounded_and_prioritizes_explicit_selection(self):
        ids = [self.hypothesis(f"待核实的备选假设 {i}") for i in range(8)]
        context = investigation_context(self.case, self.issue, "检查假设", [ids[0]])["reasoning"]
        self.assertLessEqual(len(context["hypotheses"]), 3)
        self.assertEqual(context["hypotheses"][0]["id"], ids[0])
        self.assertTrue(context["has_more"])

    def dependent_proposals(self):
        first, second = suggestion("evidence_gap"), suggestion("evidence_gap")
        first["content"], second["content"] = "核对新旧租约的计租起点", "核对实际交房时间及收款记录"
        first["hypothesis_proposal_indices"] = [0]
        second["hypothesis_proposal_indices"] = [0]
        return [suggestion("hypothesis"), first, second]

    def test_same_run_gaps_require_explicit_parent_adoption_and_are_idempotent(self):
        path = case_dir(self.case.case_id) / "issue_workspaces.json"
        response, _ = self.investigate(self.dependent_proposals())
        self.assertEqual(response.status_code, 200, response.text)
        run = response.json()["run_id"]
        audit_path = case_dir(self.case.case_id) / f"runs/{run}.output.json"
        audit_before = audit_path.read_bytes()
        before = path.read_bytes() if path.exists() else None
        blocked = self.accept(run, 1)
        self.assertEqual(blocked.status_code, 409, blocked.text)
        self.assertEqual(blocked.json()["detail"]["code"], "proposal_dependency_not_accepted")
        self.assertEqual(path.read_bytes() if path.exists() else None, before)
        parent = self.accept(run).json()["object_id"]
        gap = self.accept(run, 1)
        self.assertEqual(gap.status_code, 200, gap.text)
        again = self.accept(run, 1)
        self.assertTrue(again.json()["already_accepted"])
        self.assertEqual(again.json()["object_id"], gap.json()["object_id"])
        second = self.accept(run, 2)
        self.assertEqual(second.status_code, 200, second.text)
        state = get_issue_workspaces(self.case).workspaces[self.issue].reasoning
        self.assertEqual(len(state.hypotheses), 1)
        self.assertEqual(len(state.gaps), 2)
        self.assertTrue(all(g.hypothesis_ids == [parent] for g in state.gaps))
        self.assertEqual(audit_path.read_bytes(), audit_before)

    def test_same_index_in_another_run_cannot_satisfy_a_dependency(self):
        first, _ = self.investigate(self.dependent_proposals())
        second, _ = self.investigate(self.dependent_proposals())
        run_a, run_b = first.json()["run_id"], second.json()["run_id"]
        parent_b = self.accept(run_b).json()["object_id"]
        self.assertEqual(self.accept(run_a, 1).status_code, 409)
        parent_a = self.accept(run_a).json()["object_id"]
        gap = self.accept(run_a, 1)
        self.assertEqual(gap.status_code, 200, gap.text)
        state = get_issue_workspaces(self.case).workspaces[self.issue].reasoning
        self.assertNotEqual(parent_a, parent_b)
        self.assertEqual(state.gaps[0].hypothesis_ids, [parent_a])

    def test_returned_id_from_another_run_is_rejected_by_tool_and_adoption(self):
        first, _ = self.investigate([suggestion("hypothesis")])
        foreign_id = first.json()["proposals"][0]["proposal_id"]
        gap = suggestion("evidence_gap")
        gap["hypothesis_proposal_ids"] = [foreign_id]
        second, _ = self.investigate([gap])
        self.assertEqual(second.json()["proposals"], [])
        run = second.json()["run_id"]
        audit = json.loads((case_dir(self.case.case_id) / f"runs/{run}.output.json").read_text(encoding="utf-8"))
        self.assertEqual(audit["tool_calls"][1]["result"]["status"], "invalid_request")
        response = self.client.post(f"/api/cases/{self.case.case_id}/reasoning-proposals/accept", json={
            "issue": self.issue, "run_id": run, "proposal_id": foreign_id})
        self.assertEqual(response.status_code, 409)

    def test_copied_run_envelopes_and_wrong_target_types_are_rejected_at_adoption(self):
        response, _ = self.investigate(self.dependent_proposals())
        run = response.json()["run_id"]
        self.accept(run)
        path = case_dir(self.case.case_id) / f"runs/{run}.output.json"
        original = json.loads(path.read_text(encoding="utf-8"))
        for part in ("run_id", "result", "target_type"):
            audit = json.loads(json.dumps(original))
            if part == "run_id":
                audit["run_id"] = "investigation-" + "a" * 32
            elif part == "result":
                audit["result"]["run_id"] = "investigation-" + "a" * 32
            else:
                audit["result"]["proposals"][1]["hypothesis_proposal_ids"] = [audit["result"]["proposals"][2]["proposal_id"]]
            path.write_text(json.dumps(audit), encoding="utf-8")  # Isolated mock audit only.
            with self.subTest(part=part):
                self.assertEqual(self.accept(run, 1).status_code, 409)
        path.write_text(json.dumps(original), encoding="utf-8")

    def test_changed_parent_and_later_user_edits_invalidate_dependent_gaps(self):
        response, _ = self.investigate(self.dependent_proposals())
        run = response.json()["run_id"]
        parent = self.accept(run).json()["object_id"]
        self.assertEqual(self.accept(run, 1).status_code, 200)
        state = get_issue_workspaces(self.case).workspaces[self.issue].reasoning
        gap = state.gaps[0]
        self.edit(gap={"id": gap.id, "content": "用户修正后的核查问题", "hypothesis_ids": [parent], "status": "open"})
        stale = self.accept(run, 2)
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()["detail"]["code"], "stale_reasoning_proposal")
        fresh, _ = self.investigate(self.dependent_proposals())
        new_run = fresh.json()["run_id"]
        parent = self.accept(new_run).json()["object_id"]
        self.edit(hypothesis={"id": parent, "content": "用户修正假设", "judgment": "unverified", "work_status": "active"})
        self.assertEqual(self.accept(new_run, 1).status_code, 409)

    def test_parent_evidence_hash_is_rechecked_even_if_gap_evidence_is_unchanged(self):
        response, _ = self.investigate(self.dependent_proposals())
        run = response.json()["run_id"]
        self.accept(run)
        path = case_dir(self.case.case_id) / f"runs/{run}.output.json"
        audit = json.loads(path.read_text(encoding="utf-8"))
        audit["proposal_receipt"]["evidence_hashes"][0] = "changed-parent-evidence"
        path.write_text(json.dumps(audit), encoding="utf-8")
        response = self.accept(run, 1)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["code"], "stale_reasoning_proposal")

    def test_invalid_dependency_is_audited_without_silently_dropping_candidates(self):
        proposals = self.dependent_proposals()
        proposals[1]["hypothesis_proposal_indices"] = [5]
        response, requests = self.investigate(proposals)
        self.assertEqual(response.status_code, 200, response.text)
        run = response.json()["run_id"]
        audit = json.loads((case_dir(self.case.case_id) / f"runs/{run}.output.json").read_text(encoding="utf-8"))
        rejected = audit["tool_calls"][2]["result"]
        self.assertEqual(rejected["status"], "invalid_request")
        self.assertEqual(rejected["error"]["validation_details"]["category"], "proposal_semantics")
        self.assertEqual(rejected["error"]["validation_details"]["errors"][0]["loc"], ["proposals", 1, "hypothesis_proposal_ids", 0])
        self.assertEqual(len(response.json()["proposals"]), 2)  # The failed candidate is explicitly audited, not accepted.
        self.assertFalse(get_issue_workspaces(self.case).workspaces[self.issue].reasoning.hypotheses)

    def test_existing_ids_and_new_indices_can_be_combined_and_save_failure_is_atomic(self):
        existing = self.hypothesis()
        proposals = self.dependent_proposals()[:2]
        proposals[1]["hypothesis_ids"] = [existing]
        response, _ = self.investigate(proposals, [existing])
        self.assertEqual(response.status_code, 200, response.text)
        run = response.json()["run_id"]
        parent = self.accept(run).json()["object_id"]
        path = case_dir(self.case.case_id) / "issue_workspaces.json"
        before = path.read_bytes()
        with patch("app.issue_workspaces.write_json", side_effect=OSError("full")):
            self.assertEqual(self.accept(run, 1).status_code, 500)
        self.assertEqual(path.read_bytes(), before)
        accepted = self.accept(run, 1)
        self.assertEqual(accepted.status_code, 200, accepted.text)
        gap = get_issue_workspaces(self.case).workspaces[self.issue].reasoning.gaps[0]
        self.assertEqual(gap.hypothesis_ids, [existing, parent])

    def test_summary_failure_does_not_discard_or_auto_accept_valid_proposals(self):
        response, _ = self.investigate(self.dependent_proposals(), summary_failure=True)
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["summary_status"], "failed")
        self.assertIsNone(result["answer"])
        self.assertEqual(len(result["proposals"]), 3)
        self.assertFalse(get_issue_workspaces(self.case).workspaces[self.issue].reasoning.hypotheses)
        self.assertEqual(self.accept(result["run_id"], 1).status_code, 409)
        self.assertEqual(self.accept(result["run_id"]).status_code, 200)
        self.assertEqual(self.accept(result["run_id"], 1).status_code, 200)

    def test_ungrounded_exploration_still_requires_separate_user_approval(self):
        h = {**suggestion("hypothesis"), "info_unit_ids": [], "evidence_refs": [], "rationale": "探索性解释尚待验证。"}
        gap = {**suggestion("evidence_gap"), "info_unit_ids": [], "evidence_refs": [], "hypothesis_proposal_indices": [0]}
        response, _ = self.investigate([h, gap])
        self.assertEqual(response.status_code, 200, response.text)
        run = response.json()["run_id"]
        self.assertEqual(self.accept(run, 1).status_code, 409)
        parent = self.accept(run).json()["object_id"]
        state = get_issue_workspaces(self.case).workspaces[self.issue].reasoning
        self.assertEqual((state.hypotheses[0].judgment, state.gaps), ("unverified", []))
        adopted = self.accept(run, 1)
        self.assertEqual(adopted.status_code, 200, adopted.text)
        state = get_issue_workspaces(self.case).workspaces[self.issue].reasoning
        self.assertEqual(state.gaps[0].hypothesis_ids, [parent])
        self.assertEqual(state.gaps[0].info_unit_ids, [])
        self.assertTrue(self.accept(run, 1).json()["already_accepted"])

    def test_legacy_audit_without_proposal_ids_remains_adoptable(self):
        from app.reasoning_models import ReasoningProposal
        from app.reasoning_proposals import proposal_receipt
        old = self.dependent_proposals()[:2]
        run = "investigation-" + "a" * 32
        audit = {"run_id": run, "status": "success", "result": {"run_id": run, "proposals": old},
                 "proposal_receipt": proposal_receipt(self.case, self.issue, {"reasoning": {"hypotheses": []}},
                     [ReasoningProposal.model_validate(p) for p in old])}
        path = case_dir(self.case.case_id) / f"runs/{run}.output.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(audit), encoding="utf-8")
        before = path.read_bytes()
        self.assertEqual(self.accept(run, 1).status_code, 409)
        parent = self.accept(run).json()["object_id"]
        self.assertEqual(self.accept(run, 1).status_code, 200)
        state = get_issue_workspaces(self.case).workspaces[self.issue].reasoning
        self.assertEqual(state.gaps[0].hypothesis_ids, [parent])
        self.assertIn(f"{run}:0", state.accepted_proposals)
        self.assertEqual(path.read_bytes(), before)

    def test_tool_rejections_keep_error_locations_in_privacy_traces(self):
        import test_observability as tracing
        from app import observability
        backend = tracing.FakeBackend()
        settings = observability.ObservabilitySettings(True, False, "pk-test", "sk-test", "https://test", "test")
        proposals = self.dependent_proposals()
        proposals[1]["hypothesis_proposal_ids"] = ["proposal-" + "b" * 32]
        with patch.object(observability, "load_settings", return_value=settings), patch.object(observability, "_get_backend", return_value=backend):
            self.investigate(proposals)
        outputs = [update["output"] for span in backend.client.spans for update in span.updates if "output" in update]
        failed = next(value for value in outputs if value.get("validation_category") == "proposal_semantics")
        self.assertEqual(failed["validation_errors"][0]["loc"], ["proposals", 1, "hypothesis_proposal_ids", 0])
        self.assertNotIn(proposals[1]["content"], json.dumps(outputs, ensure_ascii=False))
