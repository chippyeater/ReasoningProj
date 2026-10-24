"""Case A UI workspace persistence and actual model-boundary checks, offline."""
import json
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

import test_investigation_agent as fixtures
from app import case_store, llm_client, main
from app.issue_workspaces import get_issue_workspaces


class TestIssueWorkspaces(unittest.TestCase):
    setUp = fixtures.TestInvestigationAgent.setUp
    def tearDown(self):
        current = self.fixture.disk_snapshot()
        for name, raw in self.fixture.before_files.items():
            self.assertEqual(current[name], raw)
        self.assertTrue(all('/runs/' in name.replace('\\', '/') or name.replace('\\', '/').endswith('/issue_workspaces.json')
                            for name in current.keys() - self.fixture.before_files.keys()))

    def test_independent_persistence_and_legacy_preservation(self):
        # Use validated Point rather than changing the business schema.
        from app.schemas import Point
        self.case.workspace_state.node_positions["U029"] = Point(x=123, y=456)
        before = self.case.model_dump_json()
        state = get_issue_workspaces(self.case)
        a, b = list(state.workspaces)[:2]
        self.assertEqual(state.workspaces[a].canvas.visible_node_ids,
                         [u.id for u in self.case.info_units if u.related_issue == a])
        self.assertLess(len(state.workspaces[a].canvas.visible_node_ids), len(self.case.info_units))
        self.assertEqual(state.workspaces[a].canvas.node_positions["U029"].x, 123)
        self.assertTrue(set(state.workspaces[b].canvas.visible_node_ids) <= state.workspaces[b].canvas.node_positions.keys())
        client = TestClient(main.app)
        with patch.object(main, "require_case", return_value=self.case):
            canvas = state.workspaces[a].canvas.model_dump(mode="json")
            canvas.update(visible_node_ids=["U029"], selected_node_ids=["U029"], focused_node_id="U029",
                          node_positions={"U029": {"x": 900, "y": 700}},
                          viewport={"zoom": 0.7, "offset_x": -90, "offset_y": 80})
            response = client.patch(f"/api/cases/{self.case.case_id}/issue-workspaces", json={
                "issue": a, "status": "closed", "canvas": canvas})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(client.patch(f"/api/cases/{self.case.case_id}/issue-workspaces", json={
                "issue": b, "status": "investigating"}).status_code, 200)
            restored = client.get(f"/api/cases/{self.case.case_id}/issue-workspaces").json()
            self.assertEqual(restored["workspaces"][a]["canvas"], canvas)
            self.assertEqual(restored["workspaces"][a]["status"], "closed")
            reopened = client.patch(f"/api/cases/{self.case.case_id}/issue-workspaces", json={"issue": a, "status": "pending"})
            self.assertEqual(reopened.json()["workspaces"][a]["status"], "pending")
        self.assertEqual(self.case.model_dump_json(), before)

    def test_invalid_input_never_overwrites_storage(self):
        client = TestClient(main.app)
        state = get_issue_workspaces(self.case)
        a = state.active_issue
        with patch.object(main, "require_case", return_value=self.case):
            url = f"/api/cases/{self.case.case_id}/issue-workspaces"
            self.assertEqual(client.patch(url, json={"issue": "foreign"}).status_code, 422)
            self.assertEqual(client.patch(url, json={"issue": a, "status": "done"}).status_code, 422)
            canvas = state.workspaces[a].canvas.model_dump(mode="json")
            canvas["visible_node_ids"] = ["foreign"]
            self.assertEqual(client.patch(url, json={"issue": a, "canvas": canvas}).status_code, 422)
            path = case_store.case_dir(self.case.case_id) / "issue_workspaces.json"
            self.assertFalse(path.exists())
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("broken", encoding="utf-8")
            self.assertEqual(client.patch(url, json={"issue": a}).status_code, 409)
            self.assertEqual(path.read_text(), "broken")

    def test_wire_context_has_status_and_bounded_semantics_without_display_state_or_sources(self):
        issue = self.case.issue_set[1]
        requests = []
        replies = [fixtures.tool("get_evidence", {"info_unit_ids": ["U029"]}),
                   fixtures.finish("核对来源", ["U029"], refs=[("U029", 0)], status="completed")]

        def model(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json=replies[len(requests) - 1].response)

        real_client = httpx.AsyncClient
        before = self.case.model_dump_json()
        with patch.object(main, "require_case", return_value=self.case), patch.object(llm_client.httpx, "AsyncClient",
                side_effect=lambda **kwargs: real_client(transport=httpx.MockTransport(model), **kwargs)):
            client = TestClient(main.app)
            client.patch(f"/api/cases/{self.case.case_id}/issue-workspaces", json={"issue": issue, "status": "investigating"})
            url = f"/api/cases/{self.case.case_id}/agent/run"
            self.assertEqual(client.post(url, json={"goal": "核对来源", "current_issue": "foreign"}).status_code, 422)
            response = client.post(url, json={"goal": "核对来源", "current_issue": issue, "selected_info_unit_ids": ["U001"]})
            self.assertEqual(response.status_code, 200, response.text)
        initial = json.loads(requests[0]["messages"][1]["content"])
        self.assertEqual(initial["core_issue"], self.case.core_issue)
        self.assertEqual(initial["work_context"]["current_issue"], issue)
        self.assertEqual(initial["work_context"]["issues"][0]["status"], "investigating")
        self.assertIn("U001", [u["id"] for u in initial["knowledge_state"]["info_units"]["items"]])
        for forbidden in ("visible_node_ids", "node_positions", "viewport", "quote_units", "files"):
            self.assertNotIn(forbidden, json.dumps(initial))
        self.assertLessEqual(len(initial["work_context"]["canvas"]["info_units"]), 8)
        self.assertLessEqual(len(initial["work_context"]["canvas"]["relations"]), 12)
        self.assertEqual(get_issue_workspaces(self.case).workspaces[issue].status, "investigating")
        self.assertEqual(self.case.model_dump_json(), before)
