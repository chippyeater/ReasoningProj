"""Exercise the HTTP entry through the real loop and payload builder, offline."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch
import httpx

import test_investigation_agent as fixtures
from fastapi.testclient import TestClient
from app import case_store, llm_client, main


class TestAgentAPI(unittest.TestCase):
    setUp = fixtures.TestInvestigationAgent.setUp
    tearDown = fixtures.TestInvestigationAgent.tearDown

    def test_latest_restores_saved_workspace_answer_and_original_sources_read_only(self):
        self._workspace_enabled = True
        issue = self.case.issue_set[0]
        replies = [fixtures.tool("get_evidence", {"info_unit_id": "U029"}), fixtures.finish("核查", status="completed")]
        with patch.object(main, "require_case", return_value=self.case), patch.object(llm_client, "call_tools", side_effect=replies) as model:
            client = TestClient(main.app)
            original = client.post(f"/api/cases/{self.case.case_id}/agent/run", json={
                "goal": "核查", "selected_info_unit_ids": ["U029"], "current_issue": issue})
            self.assertEqual(original.status_code, 200, original.text)
            saved = self.fixture.disk_snapshot()
            changed = self.case.model_copy(deep=True)
            unit = next(u for u in changed.info_units if u.id == "U029")
            unit.source_refs[0].quote = "后续修改的原文，不能替换历史依据。"
            with patch.object(main, "require_case", return_value=changed):
                restored = client.get(f"/api/cases/{self.case.case_id}/agent/latest", params={"issue": issue})
            self.assertEqual(restored.status_code, 200, restored.text)
            self.assertEqual(restored.json()["result"], original.json())
            self.assertEqual(model.call_count, 2)
            self.assertEqual(self.fixture.disk_snapshot(), saved)
            other = client.get(f"/api/cases/{self.case.case_id}/agent/latest", params={"issue": self.case.issue_set[1]})
            self.assertIsNone(other.json()["result"])
            unknown = client.get(f"/api/cases/{self.case.case_id}/agent/latest", params={"issue": "unknown"})
            self.assertEqual(unknown.status_code, 422)

    def test_latest_is_case_scoped_and_selects_creation_time_not_file_order(self):
        issue = self.case.issue_set[0]
        result = {"run_id": "investigation-" + "a" * 32, "goal": "较新结果", "answer": "保留答复",
                  "run_status": "incomplete", "evidence_refs": [], "workspace_commit": {"status": "no_change", "changes": []}}
        for suffix, timestamp, goal in (("a", "2026-11-08T08:33:31Z", "较新结果"), ("z", "2026-11-08T08:00:00Z", "较旧结果")):
            case_store.write_json(case_store.artifact_path(self.case.case_id, f"runs/investigation-{suffix}.output.json"),
                {"created_at": timestamp, "workspace_receipt": {"issue": issue}, "result": {**result, "goal": goal}})
        other_case = self.case.model_copy(update={"case_id": "case-other"})
        case_store.write_json(case_store.artifact_path(other_case.case_id, "runs/investigation-b.output.json"),
            {"created_at": "2026-11-09T08:00:00Z", "workspace_receipt": {"issue": issue}, "result": {**result, "goal": "其他案件"}})
        with patch.object(main, "require_case", return_value=self.case), patch.object(llm_client, "call_tools") as model:
            restored = TestClient(main.app).get(f"/api/cases/{self.case.case_id}/agent/latest", params={"issue": issue})
            self.assertEqual(restored.json()["result"]["goal"], "较新结果")
            self.assertEqual(restored.json()["result"]["run_status"], "incomplete")
            model.assert_not_called()

    def test_client_run_id_is_used_and_cannot_reexecute_or_overwrite_audit(self):
        run_id = "investigation-" + "a" * 32
        with patch.object(main, "require_case", return_value=self.case), patch.object(llm_client, "call_tools",
                side_effect=[fixtures.finish("核对来源", status="unsupported")]) as model:
            client = TestClient(main.app)
            payload = {"goal": "核对来源", "run_id": run_id}
            response = client.post(f"/api/cases/{self.case.case_id}/agent/run", json=payload)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["run_id"], run_id)
            output = case_store.artifact_path(self.case.case_id, f"runs/{run_id}.output.json")
            original = output.read_bytes()
            duplicate = client.post(f"/api/cases/{self.case.case_id}/agent/run", json=payload)
            self.assertEqual(duplicate.status_code, 409, duplicate.text)
            self.assertEqual(duplicate.json()["detail"]["code"], "duplicate_investigation_run")
            self.assertEqual(output.read_bytes(), original)
            self.assertEqual(model.call_count, 1)

            # A failed earlier Run may have only an output artifact. Protect it too.
            case_store.artifact_path(self.case.case_id, f"runs/{run_id}.input.json").unlink()
            duplicate = client.post(f"/api/cases/{self.case.case_id}/agent/run", json=payload)
            self.assertEqual(duplicate.status_code, 409, duplicate.text)
            self.assertEqual(output.read_bytes(), original)
            self.assertEqual(model.call_count, 1)

    def test_client_run_id_rejects_invalid_paths_before_model_call(self):
        with patch.object(main, "require_case", return_value=self.case), patch.object(llm_client, "call_tools") as model:
            for run_id in ("../other", "investigation-wrong", "investigation-" + "a" * 33):
                response = TestClient(main.app).post(f"/api/cases/{self.case.case_id}/agent/run",
                    json={"goal": "核查", "run_id": run_id})
                self.assertEqual(response.status_code, 422)
            model.assert_not_called()
    def test_http_runs_dynamic_loop_with_bounded_initial_wire_request(self):
        goal = "核对损失说法"
        requests = []
        replies = [fixtures.tool("get_evidence", {"info_unit_id": "U029"}),
                   fixtures.finish(goal, status='completed')]

        def model(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json=replies[len(requests) - 1].response)

        real_client = httpx.AsyncClient
        with patch.object(main, "require_case", return_value=self.case), patch.object(llm_client.httpx, "AsyncClient",
                side_effect=lambda **kwargs: real_client(transport=httpx.MockTransport(model), **kwargs)):
            response = TestClient(main.app).post(f"/api/cases/{self.case.case_id}/agent/run", json={
                "goal": goal, "selected_info_unit_ids": ["U029"]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["completion_status"], "completed")
        self.assertNotIn("response_format", requests[0])
        initial = json.loads(requests[0]["messages"][1]["content"])
        memory = initial["materials"]
        self.assertEqual(memory["source_refs"]["items"], [])
        obtained = json.loads(requests[1]["messages"][1]["content"])["materials"]["source_refs"]["items"]
        self.assertEqual(obtained[0]["source_ref_index"], 0)
        self.assertEqual(obtained[0]["source_ref"]["quote"], next(u for u in self.case.info_units if u.id == "U029").source_refs[0].quote)
        self.assertNotIn("quote_units", json.dumps(initial))
        self.assertEqual(len(memory["info_units"]["items"]), 1)
        self.assertEqual(response.json()["evidence_refs"], [{"info_unit_id": "U029", "source_ref_index": i} for i in (0, 1)])
        self.assertNotIn("findings", response.json())
        ref = next(unit for unit in self.case.info_units if unit.id == "U029").source_refs[0]
        self.assertEqual(response.json()["evidence"][0]["quote"], ref.quote)
        self.assertEqual(response.json()["evidence"][0]["start"], ref.start)
        self.assertNotIn("planned_actions", json.dumps(response.json()))
        definitions = {d["function"]["name"]: d["function"] for d in requests[0]["tools"]}
        self.assertEqual(set(definitions), {"search_info_units", "get_relations", "get_evidence",
                                           "propose_hypothesis", "propose_evidence_gap", "finish_investigation"})
        self.assertNotIn("case_info_directory", initial)
        self.assertNotIn("core_issue", initial)
        self.assertNotIn("obtained_source_ref_indices", memory["info_units"]["items"][0])
        self.assertNotIn("update_investigation_steps", definitions)
        self.assertNotIn("question_state", response.json())
        self.assertNotIn("phase", response.json())
        self.assertEqual(requests[0]["tool_choice"], "auto")
        self.assertTrue(all(len(r["messages"]) == 2 for r in requests))

    def test_http_unsupported_is_explicit_and_does_not_read_or_modify_case(self):
        goal = "删除这个节点"
        replies = [fixtures.finish(goal, status="unsupported", questions=["当前仅支持只读调查；请使用手动编辑功能。"])]
        with patch.object(main, "require_case", return_value=self.case), patch.object(llm_client, "call_tools", side_effect=replies):
            response = TestClient(main.app).post(f"/api/cases/{self.case.case_id}/agent/run", json={
                "goal": goal, "selected_info_unit_ids": ["U022"]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["completion_status"], "unsupported")
        self.assertEqual(response.json()["tool_budget_used"]["read"], 0)

    def test_http_errors_are_not_results(self):
        with patch.object(main, "require_case", return_value=self.case), patch.object(llm_client, "call_tools", side_effect=
                llm_client.LLMFailure("llm_timeout", "Mock timeout")):
            client = TestClient(main.app)
            response = client.post(f"/api/cases/{self.case.case_id}/agent/run", json={"goal": "核对来源"})
            self.assertEqual(response.status_code, 502)
            self.assertEqual(response.json()["detail"]["code"], "llm_timeout")
            self.assertEqual(client.post(f"/api/cases/{self.case.case_id}/agent/run", json={"goal": " "}).status_code, 422)
