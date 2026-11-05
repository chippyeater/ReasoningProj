"""Real HTTP/model payload boundary with mock completions, whole-run automatic commit."""
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
            "hypothesis_ids": [hid] if hid else [],
            ("basis_info_unit_ids" if kind == "evidence_gap" else "info_unit_ids"): ["U029"],
            "meaning": "oppose" if kind == "evidence_link" else None,
            "judgment": "unverified" if kind == "hypothesis_status" else None,
            "rationale": "张某陈述无损失，仍应核对入住和收租材料。",
            "evidence_refs": [{"info_unit_id": "U029", "source_ref_index": 0}]}


def proposal_call(source, prior_ids, call_id):
    """Offline scenario -> native call, using only IDs returned by previous tools."""
    kind = source["kind"]
    args = {key: source[key] for key in ("rationale", "evidence_refs")}
    if kind == "evidence_gap_update":
        args.update(gap_id=source["gap_id"], addressing_info_unit_ids=source["addressing_info_unit_ids"], status=source["status"])
    elif kind == "evidence_link":
        args.update(hypothesis_id=source["hypothesis_ids"][0] if source["hypothesis_ids"] else None,
                    info_unit_id=source["info_unit_ids"][0], meaning=source["meaning"],
                    hypothesis_proposal_ids=source.get("hypothesis_proposal_ids", [
                        prior_ids[index] if index < len(prior_ids) else "proposal-" + "f" * 32
                        for index in source.get("hypothesis_proposal_indices", [])]))
    else:
        field = "basis_info_unit_ids" if kind == "evidence_gap" else "info_unit_ids"
        args[field] = source[field]
        if kind in {"hypothesis", "evidence_gap"}:
            args["content"] = source["content"]
        if kind == "hypothesis_status":
            args.update(hypothesis_id=source["hypothesis_ids"][0], judgment=source["judgment"])
        if kind == "evidence_gap":
            args.update(hypothesis_targets={"hypothesis_ids": source["hypothesis_ids"], "hypothesis_proposal_ids": source.get("hypothesis_proposal_ids", [
                prior_ids[index] if index < len(prior_ids) else "proposal-" + "f" * 32
                for index in source.get("hypothesis_proposal_indices", [])])})
    return replies.tool("propose_" + kind, args, call_id)


class TestReasoningProposals(TestReasoningWorkspace):
    # Do not repeat parent tests in this suite.
    test_edit_link_gap_and_restore_without_business_copies = None
    test_invalid_refs_duplicates_and_write_failure_leave_file_unchanged = None
    test_legacy_workspace_and_referenced_evidence_are_protected = None

    def investigate(self, proposals, selected=None, *, summary_failure=False, read_ids=None, before_finish=None):
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
                state = json.loads(body["messages"][1]["content"])
                ids = [p["proposal_id"] for p in state["proposals"]]
                reply = proposal_call(proposals[step - 1], ids, f"proposal-{step}")
            elif summary_failure:
                return httpx.Response(500, text="mock final model failure")
            else:
                if before_finish:
                    before_finish()
                reply = replies.finish(goal, status='completed')
            return httpx.Response(200, json=reply.response)
        with patch.object(llm_client.httpx, "AsyncClient", side_effect=lambda **kwargs:
                          real_client(transport=httpx.MockTransport(model), **kwargs)):
            response = self.client.post(f"/api/cases/{self.case.case_id}/agent/run", json={
                "goal": goal, "current_issue": self.issue, "selected_reasoning_ids": selected or []})
        return response, requests


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


    def test_duplicate_formal_hypothesis_omitted_from_model_projection_is_rejected_without_write(self):
        content = "租约始终连续计租，没有实际租金断档"
        hidden = self.hypothesis(content)
        visible = [self.hypothesis(f"独立解释条件{index}") for index in range(3)]
        context = investigation_context(self.case, self.issue, "其他事项", selected_reasoning_ids=visible)
        self.assertNotIn(hidden, {h["id"] for h in context["reasoning"]["hypotheses"]})
        path = case_dir(self.case.case_id) / "issue_workspaces.json"
        before = path.read_bytes()
        proposal = {**suggestion("hypothesis"), "content": content + "。"}
        response, requests = self.investigate([proposal], selected=visible)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["proposals"], [])
        recovery = json.loads(requests[2]["messages"][1]["content"])["recovery"]
        existing = recovery["next_action"]["steps"][0]["existing_object"]
        self.assertEqual(existing["id"], hidden)
        self.assertEqual(existing["source"], "reasoning")
        self.assertEqual(path.read_bytes(), before)


    def test_invalid_dependency_is_audited_without_silently_dropping_candidates(self):
        proposals = self.dependent_proposals()
        proposals[1]["hypothesis_proposal_indices"] = [5]
        response, requests = self.investigate(proposals)
        self.assertEqual(response.status_code, 200, response.text)
        run = response.json()["run_id"]
        audit = json.loads((case_dir(self.case.case_id) / f"runs/{run}.output.json").read_text(encoding="utf-8"))
        rejected = next(c for c in audit["tool_calls"] if c["result"]["status"] == "invalid_request")["result"]
        self.assertEqual(rejected["status"], "invalid_request")
        self.assertEqual(rejected["error"]["validation_details"]["category"], "proposal_semantics")
        self.assertEqual(rejected["error"]["validation_details"]["errors"][0]["loc"], ["proposals", 1, "hypothesis_proposal_ids", 0])
        self.assertEqual(len(response.json()["proposals"]), 2)  # The failed candidate is explicitly audited, not accepted.
        self.assertEqual(len(get_issue_workspaces(self.case).workspaces[self.issue].reasoning.hypotheses), 1)


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
