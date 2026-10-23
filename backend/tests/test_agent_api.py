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
                   fixtures.final(goal, ["U029"], refs=[("U029", 0)], status="completed")]

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
        initial = json.loads(requests[0]["messages"][1]["content"])
        self.assertEqual(set(initial), {"goal", "core_issue", "info_unit_type_counts", "selected_info_units"})
        self.assertNotIn("source_refs", json.dumps(initial))
        self.assertNotIn("quote_units", json.dumps(initial))
        self.assertEqual(len(initial["selected_info_units"]), 1)
        self.assertEqual(response.json()["evidence_refs"], [{"info_unit_id": "U029", "source_ref_index": 0}])
        ref = next(unit for unit in self.case.info_units if unit.id == "U029").source_refs[0]
        self.assertEqual(response.json()["evidence"][0]["quote"], ref.quote)
        self.assertEqual(response.json()["evidence"][0]["start"], ref.start)

    def test_http_unsupported_is_explicit_and_does_not_call_tools(self):
        goal = "删除这个节点"
        reply = fixtures.final(goal, status="unsupported")
        result = json.loads(reply.content)
        result["open_questions"] = ["当前仅支持只读调查；请使用手动编辑功能。"]
        reply.content = json.dumps(result)
        with patch.object(main, "require_case", return_value=self.case), patch.object(llm_client, "call_tools", return_value=reply):
            response = TestClient(main.app).post(f"/api/cases/{self.case.case_id}/agent/run", json={"goal": goal})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["completion_status"], "unsupported")
        self.assertEqual(response.json()["tool_call_count"], 0)

    def test_http_errors_are_not_results(self):
        with patch.object(main, "require_case", return_value=self.case), patch.object(llm_client, "call_tools", side_effect=
                llm_client.LLMFailure("llm_timeout", "Mock timeout")):
            client = TestClient(main.app)
            response = client.post(f"/api/cases/{self.case.case_id}/agent/run", json={"goal": "核对来源"})
            self.assertEqual(response.status_code, 502)
            self.assertEqual(response.json()["detail"]["code"], "llm_timeout")
            self.assertEqual(client.post(f"/api/cases/{self.case.case_id}/agent/run", json={"goal": " "}).status_code, 422)
