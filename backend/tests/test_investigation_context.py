"""Bounded Issue semantics at the real HTTP/model request boundary, offline."""
import json
import unittest
from unittest.mock import patch

import httpx
import test_investigation_agent as replies
import test_reasoning_workspace as fixture
from app import llm_client
from app.errors import AppError
from app.issue_workspaces import investigation_context, get_issue_workspaces, persist_issue_workspaces
from app.schemas import Relation


class TestInvestigationContext(unittest.TestCase):
    setUp = fixture.TestReasoningWorkspace.setUp
    tearDown = fixture.TestReasoningWorkspace.tearDown
    edit = fixture.TestReasoningWorkspace.edit
    hypothesis = fixture.TestReasoningWorkspace.hypothesis

    def canvas(self, ids, selected=(), focus=None):
        state = get_issue_workspaces(self.case)
        canvas = state.workspaces[self.issue].canvas
        canvas.visible_node_ids = list(ids)
        canvas.selected_node_ids, canvas.focused_node_id = list(selected), focus
        persist_issue_workspaces(self.case, state)

    def test_actual_request_contains_conflict_direction_focus_and_user_judgments_not_layout(self):
        h = self.hypothesis("尚不能确认租金损失")
        self.edit(link={"hypothesis_id": h, "info_unit_id": "U029", "meaning": "oppose", "rationale": "当事人陈述尚未核实"})
        self.edit(gap={"content": "实际计租和收款如何", "hypothesis_ids": [h], "info_unit_ids": ["U036"], "status": "open"})
        self.canvas(["U029", "U036", h], ["U029"], h)
        requests = []
        responses = [replies.plan(), replies.finish("核查损失", ["U029", "U036"], ["R010"], [("U029", 0), ("U036", 0)],
            statement="双方陈述存在分歧，仍需独立核实。", step_updates=[replies.step(status="completed", note="已核对上下文陈述", basis=["U029", "U036"])])]
        def model(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json=responses[len(requests) - 1].response)
        real_client = httpx.AsyncClient
        with patch.object(llm_client.httpx, "AsyncClient", side_effect=lambda **kwargs:
                          real_client(transport=httpx.MockTransport(model), **kwargs)):
            response = self.client.post(f"/api/cases/{self.case.case_id}/agent/run", json={
                "goal": "核查损失", "current_issue": self.issue, "selected_info_unit_ids": ["U029"],
                "selected_reasoning_ids": [h], "focused_object_id": h})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["relevant_relation_ids"], ["R010"])
        initial = json.loads(requests[0]["messages"][1]["content"])
        context = initial["work_context"]
        self.assertEqual([u["id"] for u in context["canvas"]["info_units"]], ["U029", "U036"])
        relation = next(r for r in self.case.relations if r.id == "R010")
        self.assertEqual(context["canvas"]["relations"], [{"id": "R010", "source_id": relation.source_id,
            "target_id": relation.target_id, "relation_type": "contradict"}])
        self.assertEqual(context["canvas"]["focused_object_id"], h)
        self.assertEqual(context["canvas"]["selected_reasoning_ids"], [h])
        self.assertEqual(context["reasoning"]["hypotheses"][0]["judgment"], "unverified")
        self.assertEqual(context["reasoning"]["gaps"][0]["status"], "open")
        self.assertEqual(context["reasoning"]["links"][0]["meaning"], "oppose")
        serialized = json.dumps(initial["work_context"])
        for field in ("node_positions", "viewport", "offset_x", "display_levels", "source_refs", "quote_units", "files"):
            self.assertNotIn(field, serialized)
        self.assertEqual(len(response.json()["evidence_refs"]), 2)  # Verified initial excerpts are directly citable.

    def test_large_canvas_prioritizes_selection_focus_and_goal_and_marks_omissions(self):
        self.canvas([u.id for u in self.case.info_units], ["U036"], "U029")
        # Many persisted edges exercise the relation cap and endpoint filtering.
        for index in range(16):
            self.case.relations.append(Relation.model_validate({**self.case.relations[0].model_dump(),
                "id": f"context-edge-{index}", "source_id": "U029", "target_id": "U036"}))
        before = self.case.model_dump_json()
        context = investigation_context(self.case, self.issue, "入住", selected_info_unit_ids=["U036"], focused_object_id="U029")
        canvas = context["canvas"]
        self.assertEqual([u["id"] for u in canvas["info_units"][:2]], ["U036", "U029"])
        self.assertEqual(len(canvas["info_units"]), 8)
        self.assertEqual(len(canvas["relations"]), 12)
        self.assertTrue(canvas["has_more_info_units"])
        self.assertTrue(canvas["has_more_relations"])
        uids = {u["id"] for u in canvas["info_units"]}
        self.assertTrue(all({r["source_id"], r["target_id"]} <= uids for r in canvas["relations"]))
        self.assertTrue(all(len(u["content"]) <= 120 for u in canvas["info_units"]))
        self.assertEqual(self.case.model_dump_json(), before)
        # Fixture checks original CaseData at teardown; additions were for this test only.
        self.case.relations = [r for r in self.case.relations if not r.id.startswith("context-edge-")]

    def test_hidden_foreign_objects_and_other_issue_reasoning_are_not_injected(self):
        h = self.hypothesis("当前议题的假设")
        self.canvas(["U029", h], focus=h)
        context = investigation_context(self.case, self.issue)
        self.assertEqual([u["id"] for u in context["canvas"]["info_units"]], ["U029"])
        self.assertEqual(context["canvas"]["relations"], [])
        other = investigation_context(self.case, self.case.core_issue)
        self.assertEqual(other["reasoning"]["hypotheses"], [])
        for kwargs in ({"selected_info_unit_ids": ["foreign"]}, {"focused_object_id": "U036"},
                       {"selected_reasoning_ids": ["foreign"]}):
            with self.subTest(kwargs=kwargs), self.assertRaises(AppError):
                investigation_context(self.case, self.issue, **kwargs)


if __name__ == "__main__":
    unittest.main()
