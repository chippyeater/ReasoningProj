"""Incremental evidence reaches actual Agent requests without changing the Canvas."""
from copy import deepcopy
import json
from unittest.mock import AsyncMock, patch

from support import IsolatedCaseTest, model_result, model_unit, quote_id_for
import test_extraction_api as extraction_fixture
import test_investigation_agent as agent_fixture
from app import case_store, extraction_service, issue_workspaces, investigation_tools, llm_client
from app.issue_workspaces import get_issue_workspaces, persist_issue_workspaces


class TestIncrementalInvestigationContext(IsolatedCaseTest):
    run_with_reply = extraction_fixture.TestExtractionAPI.run_with_reply

    def setUp(self):
        super().setUp()
        self.case_dict = self.upload(raw="甲方陈述付款情况".encode("utf-8"), source_kind="testimony")
        fid = self.case_dict["files"][0]["file_id"]
        quote = quote_id_for(self.case_dict["case_id"], fid, "甲方陈述付款情况")
        self.issue, self.other = "款项是否已支付", "另一独立争点"
        units = [model_unit(fid, quote, content="旧可见付款陈述。"),
                 model_unit(fid, quote, content="旧隐藏付款陈述。"),
                 model_unit(fid, quote, content="其他议题的陈述。", related_issue=self.other)]
        payload = json.loads(model_result(units))
        payload["issue_set"].append(self.other)
        response, _ = self.run_with_reply(self.case_dict, json.dumps(payload, ensure_ascii=False))
        self.assertEqual(response.status_code, 200, response.text)
        self.case = case_store.load_case(self.case_dict["case_id"])
        self.visible, self.hidden, self.foreign = [u.id for u in self.case.info_units]
        state = get_issue_workspaces(self.case).model_dump(mode="json")
        canvas = state["workspaces"][self.issue]["canvas"]
        canvas.update(visible_node_ids=[self.visible], selected_node_ids=[],
                      focused_node_id=None, node_positions={self.visible: {"x": 701, "y": 349}},
                      display_levels={self.visible: 2}, viewport={"zoom": 0.8, "offset_x": -60, "offset_y": 70})
        self.original_canvases = {key: deepcopy(item["canvas"]) for key, item in state["workspaces"].items()}
        self.path = self.data_dir / self.case.case_id / "issue_workspaces.json"
        case_store.write_json(self.path, state)

    def append(self):
        response = self.client.post(f"/api/cases/{self.case.case_id}/files", data={
            "source_kinds": '{"new.txt":"testimony"}'}, files={"files": ("new.txt", "补充付款记录".encode("utf-8"))})
        self.assertEqual(response.status_code, 200, response.text)
        case = response.json()["case"]
        fid = case["files"][-1]["file_id"]
        return fid, quote_id_for(self.case.case_id, fid, "补充付款记录")

    def extract(self, units):
        payload = json.loads(model_result(units))
        payload["issue_set"].append(self.other)
        response, _ = self.run_with_reply(self.case_dict, json.dumps(payload, ensure_ascii=False))
        self.assertEqual(response.status_code, 200, response.text)
        self.case = case_store.load_case(self.case.case_id)
        return response

    def actual_context(self, issue=None, goal="核对付款", selected=(), focus=None):
        requests = []
        async def model(config, messages, definitions, schema=None, **kwargs):
            state = json.loads(messages[1]["content"])
            requests.append(state)
            return agent_fixture.finish(goal, status="completed", basis="general", answer="上下文检查完成。")
        with patch.object(llm_client, "load_config", return_value=agent_fixture.CONFIG), patch.object(
                llm_client, "call_tools", new=AsyncMock(side_effect=model)):
            response = self.client.post(f"/api/cases/{self.case.case_id}/agent/run", json={
                "goal": goal, "current_issue": issue or self.issue,
                "selected_info_unit_ids": list(selected), "focused_object_id": focus})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(requests), 1, response.text)
        return requests[0]

    def ids(self, state):
        return [u["id"] for u in state["materials"]["info_units"]["items"]]

    def assert_canvases_unchanged(self):
        current = get_issue_workspaces(case_store.load_case(self.case.case_id))
        for key, canvas in self.original_canvases.items():
            self.assertEqual(current.workspaces[key].canvas.model_dump(mode="json"), canvas)
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        for key, canvas in self.original_canvases.items():
            self.assertEqual(saved["workspaces"][key]["canvas"], canvas)

    def test_visible_nodes_and_new_batch_never_populate_first_turn(self):
        fid, quote = self.append()
        before = self.path.read_bytes()
        self.extract([model_unit(fid, quote, content="新增付款记录。", related_issue=self.other)])
        state = self.actual_context()
        self.assertEqual(self.ids(state), [])
        self.assertEqual(state["materials"]["source_refs"]["items"], [])
        self.assertEqual(state["materials"]["relations"]["items"], [])
        self.assertNotIn("recent_extraction", state)
        self.assertEqual(state["case_info_directory"]["total_count"], 4)
        self.assertTrue(all(set(row) == {"id", "title", "type", "title_truncated"}
                            for row in state["case_info_directory"]["items"]))
        self.assertEqual(self.path.read_bytes(), before)
        self.assert_canvases_unchanged()

    def test_only_explicit_selection_and_focus_supply_summaries_not_sources(self):
        state = self.actual_context(selected=[self.hidden], focus=self.visible)
        self.assertEqual(set(self.ids(state)), {self.hidden, self.visible})
        self.assertNotIn(self.foreign, self.ids(state))
        self.assertEqual(state["materials"]["source_refs"]["items"], [])
        self.assertTrue(all(row["obtained_source_ref_indices"] == [] for row in state["materials"]["info_units"]["items"]))
        self.assert_canvases_unchanged()

    def test_search_prioritizes_new_nodes_and_new_sources_on_old_nodes(self):
        fid, quote = self.append()
        self.extract([model_unit(fid, quote, content="旧可见付款陈述。"),
                      model_unit(fid, quote, content="新增付款记录。", related_issue=self.other)])
        new = self.case.info_units[-1].id
        self.assertEqual(len(self.case.info_units), 4)
        self.assertEqual(self.ids(self.actual_context()), [])
        response = investigation_tools.search_info_units(self.case, "付款", limit=2)
        self.assertEqual({row["id"] for row in response["items"]}, {self.visible, new})
        self.assertTrue(response["has_more"])
        refs = investigation_tools.get_evidence(self.case, [self.visible])["items"]
        self.assertEqual(refs[0]["source_ref_index"], 1)
        self.assertEqual(refs[0]["source_ref"]["file_id"], fid)
        self.assertEqual(self.ids(self.actual_context()), [])
        self.assert_canvases_unchanged()

    def test_extraction_does_not_depend_on_a_workspace_write(self):
        fid, quote = self.append()
        before = self.path.read_bytes()
        with patch.object(issue_workspaces, "write_json", side_effect=OSError("workspace read only")) as write:
            self.extract([model_unit(fid, quote, content="新增付款记录。")])
        write.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)
        result = investigation_tools.search_info_units(self.case, "付款", limit=1)
        self.assertEqual(result["items"][0]["id"], self.case.info_units[-1].id)
        self.assertEqual(self.ids(self.actual_context()), [])
        self.assert_canvases_unchanged()

    def test_failed_case_commit_never_exposes_new_material_or_changes_canvas(self):
        fid, quote = self.append()
        original = extraction_service.save_case
        def save(candidate, **kwargs):
            if candidate.stage_runs[-1].status.value == "success":
                raise OSError("case commit failed")
            return original(candidate, **kwargs)
        before = self.path.read_bytes()
        with patch.object(extraction_service, "save_case", side_effect=save):
            result, _ = self.run_with_reply(self.case_dict, model_result([model_unit(fid, quote, content="未提交新增付款记录。")]))
        self.assertEqual(result.status_code, 500, result.text)
        stored = case_store.load_case(self.case.case_id)
        self.assertEqual(len(stored.info_units), 3)
        self.assertNotIn(fid, investigation_tools.recent_extraction_files(stored))
        self.assertEqual(self.path.read_bytes(), before)
        self.assert_canvases_unchanged()

    def test_latest_success_only_and_invalid_batch_scope_fails_explicitly(self):
        fid, quote = self.append()
        self.extract([model_unit(fid, quote, content="新增付款记录。")])
        latest = self.case.stage_runs[-1]
        self.case.stage_runs.append(latest.model_copy(update={"id": "failed-run", "status": type(latest.status).failed}))
        self.assertEqual(investigation_tools.recent_extraction_files(self.case), {fid})
        path = self.data_dir / self.case.case_id / latest.input_snapshot_path
        path.write_text('{}', encoding="utf-8")
        result = investigation_tools.search_info_units(self.case, "付款")
        self.assertEqual(result["status"], "invalid_request")
        self.assertEqual(result["error"]["code"], "extraction_scope_unavailable")
        self.assertEqual(result["items"], [])

    def test_repeated_workspace_save_contains_only_current_fields_and_keeps_canvas(self):
        for _ in range(2):
            persist_issue_workspaces(self.case, get_issue_workspaces(self.case))
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(set(saved), {"active_issue", "workspaces"})
        self.assertTrue(all(set(item) == {"status", "canvas", "reasoning"} for item in saved["workspaces"].values()))
        self.assert_canvases_unchanged()
