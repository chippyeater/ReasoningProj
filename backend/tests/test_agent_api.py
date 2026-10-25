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
    def test_http_runs_dynamic_loop_with_bounded_initial_wire_request(self):
        goal = "核对损失说法"
        requests = []
        replies = [fixtures.tool("get_evidence", {"info_unit_ids": ["U029"]}),
                   fixtures.finish(goal, ["U029"], refs=[("U029", 0)], status="completed")]

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
        memory = initial["knowledge_state"]
        self.assertEqual(len(memory["source_refs"]["items"]), 1)
        self.assertTrue(memory["source_refs"]["items"][0]["verified_against_cached_text"])
        self.assertLessEqual(len(memory["source_refs"]["items"][0]["quote_excerpt"]), 160)
        self.assertNotIn("quote_units", json.dumps(initial))
        self.assertEqual(len(memory["info_units"]["items"]), 1)
        self.assertEqual(response.json()["evidence_refs"], [{"info_unit_id": "U029", "source_ref_index": 0}])
        ref = next(unit for unit in self.case.info_units if unit.id == "U029").source_refs[0]
        self.assertEqual(response.json()["evidence"][0]["quote"], ref.quote)
        self.assertEqual(response.json()["evidence"][0]["start"], ref.start)
        self.assertNotIn("planned_actions", json.dumps(response.json()))
        definitions = {d["function"]["name"]: d["function"] for d in requests[0]["tools"]}
        self.assertIn("update_investigation_steps", definitions)
        self.assertNotIn("PlannedAction", json.dumps(definitions))
        self.assertNotIn("pending_action_ids", json.dumps(initial))
        step_fields = definitions["update_investigation_steps"]["parameters"]["properties"]["steps"]["items"]["properties"]
        self.assertNotIn("activity_indices", step_fields)
        self.assertNotIn("activity_start_index", step_fields)
        self.assertIn("basis_info_unit_ids", step_fields)
        finish_schema = definitions["finish_investigation"]["parameters"]
        self.assertIn("step_updates", finish_schema["required"])
        self.assertEqual(finish_schema["properties"]["step_updates"]["items"]["properties"], step_fields)
        self.assertEqual(response.json()["question_state"]["investigation_steps"], [])

    def test_http_step_plan_uses_same_model_loop_and_returns_server_activity(self):
        replies = [fixtures.tool("update_investigation_steps", {"steps": [fixtures.step()]}, "plan"),
            fixtures.tool("get_evidence", {"info_unit_ids": ["U029"], "step_id": "S1"}, "read"),
            fixtures.finish("核对", ["U029"], refs=[("U029", 0)],
                step_updates=[fixtures.step(status="completed", note="已核对来源，仍需独立佐证")])]
        requests = []
        def model(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json=replies[len(requests) - 1].response)
        real_client = httpx.AsyncClient
        with patch.object(main, "require_case", return_value=self.case), patch.object(llm_client.httpx, "AsyncClient",
                side_effect=lambda **kwargs: real_client(transport=httpx.MockTransport(model), **kwargs)):
            response = TestClient(main.app).post(f"/api/cases/{self.case.case_id}/agent/run", json={"goal": "核对"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["question_state"]["investigation_steps"][0]["activity_indices"], [1])
        self.assertEqual(response.json()["question_state"]["investigation_steps"][0]["status"], "completed")
        self.assertEqual(response.json()["tool_budget_used"]["question"], 1)
        self.assertEqual(response.json()["proposals"], [])
        state = json.loads(requests[2]["messages"][1]["content"])
        self.assertEqual(state["investigation_steps"]["progress"][0]["status"], "in_progress")

    def test_http_unsupported_is_explicit_and_does_not_read_or_modify_case(self):
        goal = "删除这个节点"
        replies = [fixtures.finish(goal, status="unsupported", questions=["当前仅支持只读调查；请使用手动编辑功能。"])]
        with patch.object(main, "require_case", return_value=self.case), patch.object(llm_client, "call_tools", side_effect=replies):
            response = TestClient(main.app).post(f"/api/cases/{self.case.case_id}/agent/run", json={"goal": goal})
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
