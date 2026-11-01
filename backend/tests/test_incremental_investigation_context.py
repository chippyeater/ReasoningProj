"""Incremental evidence reaches actual Agent requests without changing the Canvas."""
from copy import deepcopy
from itertools import combinations
import json
from unittest.mock import AsyncMock, patch

from support import IsolatedCaseTest, model_result, model_unit, quote_id_for
import test_extraction_api as extraction_fixture
import test_investigation_agent as agent_fixture
from app import case_store, extraction_service, issue_workspaces, llm_client
from app.issue_workspaces import get_issue_workspaces, investigation_context, persist_issue_workspaces
from app.schemas import Relation


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
        canvas.update(visible_node_ids=[self.visible], selected_node_ids=[self.visible],
                      focused_node_id=self.visible, node_positions={self.visible: {"x": 701, "y": 349}},
                      display_levels={self.visible: 2}, viewport={"zoom": 0.8, "offset_x": -60, "offset_y": 70})
        self.original_canvases = {key: deepcopy(item["canvas"]) for key, item in state["workspaces"].items()}
        # A real historical workspace, without either investigation field.
        state.pop("investigation_baseline_info_unit_ids")
        for item in state["workspaces"].values():
            item.pop("investigation_info_unit_ids")
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
            units = [u["id"] for u in state["materials"]["info_units"]["items"]]
            relations = [r["id"] for r in state["materials"]["relations"]["items"]]
            refs = [(ref["info_unit_id"], ref["source_ref_index"])
                    for ref in state["materials"]["source_refs"]["items"]]
            return agent_fixture.finish(goal, units, relations, refs)
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

    def test_persisted_issue_scope_stays_isolated_and_recent_batch_is_separately_available(self):
        fid, quote = self.append()
        self.extract([model_unit(fid, quote, content="本议题的新付款记录。"),
                      model_unit(fid, quote, content="其他议题的新记录。", related_issue=self.other)])
        new, foreign_new = [u.id for u in self.case.info_units[3:]]
        self.case.relations.extend([
            Relation(id="new-relation", source_id=self.visible, target_id=new,
                     relation_type="support", evidence_unit_ids=[new], rationale="同一议题的补充记载", certainty="explicit"),
            Relation(id="foreign-relation", source_id=new, target_id=foreign_new,
                     relation_type="about", evidence_unit_ids=[new], rationale="跨议题记录", certainty="explicit")])
        case_store.save_case(self.case, set_current=False)
        request = self.actual_context()
        self.assertEqual(set(self.ids(request)), {self.visible, new, foreign_new})
        self.assertEqual({r["id"] for r in request["materials"]["relations"]["items"]}, {"new-relation", "foreign-relation"})
        self.assertEqual({s["info_unit_id"] for s in request["materials"]["source_refs"]["items"]}, {self.visible, new, foreign_new})
        self.assertEqual(set(self.ids(self.actual_context(self.other))), {self.foreign, foreign_new, new})
        scopes = get_issue_workspaces(self.case).workspaces
        self.assertEqual(set(scopes[self.issue].investigation_info_unit_ids), {self.visible, new})
        self.assertEqual(set(scopes[self.other].investigation_info_unit_ids), {self.foreign, foreign_new})
        self.assert_canvases_unchanged()

    def test_historical_workspace_read_preserves_old_scope_and_does_not_write(self):
        # Imported historical workspaces may have no Extraction batch record.
        self.case.stage_runs = [r for r in self.case.stage_runs if r.stage.value != "extraction"]
        case_store.save_case(self.case, set_current=False)
        before = self.path.read_bytes()
        self.assertEqual(self.ids(self.actual_context()), [self.visible])
        self.assertNotIn(self.hidden, self.ids(self.actual_context()))
        self.assertEqual(self.path.read_bytes(), before)
        projected = get_issue_workspaces(self.case)
        self.assertEqual(projected.workspaces[self.issue].investigation_info_unit_ids, [self.visible])
        self.assertEqual(set(projected.investigation_baseline_info_unit_ids), {self.visible, self.hidden, self.foreign})
        self.assert_canvases_unchanged()

    def test_source_merge_and_repeated_sync_do_not_create_duplicate_ids(self):
        fid, quote = self.append()
        self.extract([model_unit(fid, quote, content="旧可见付款陈述。")])
        self.assertEqual(len(self.case.info_units), 3)
        state = get_issue_workspaces(self.case)
        persist_issue_workspaces(self.case, state)
        before = self.path.read_bytes()
        persist_issue_workspaces(self.case, get_issue_workspaces(self.case))
        self.assertEqual(self.path.read_bytes(), before)
        request = self.actual_context()
        self.assertEqual(self.ids(request), [self.visible])
        self.assertEqual(len(self.case.info_units[0].source_refs), 2)
        # Still one reference per node, now prioritizing the latest batch source.
        self.assertEqual([(r["info_unit_id"], r["source_ref_index"])
                          for r in request["materials"]["source_refs"]["items"]], [(self.visible, 1)])
        self.assert_canvases_unchanged()

    def test_new_evidence_stays_bounded_prioritizes_selection_focus_goal_and_marks_omissions(self):
        fid, quote = self.append()
        self.extract([model_unit(fid, quote, title=f"补充节点{index}",
                      content="入住实际计租条件有补充记载。" if index == 0 else f"付款补充记录第{index}项。") for index in range(10)])
        new_ids = [u.id for u in self.case.info_units[3:]]
        initial = investigation_context(self.case, self.issue, "入住实际计租", selected_info_unit_ids=[new_ids[-1]],
                                        focused_object_id=self.visible)["canvas"]
        pairs = list(combinations([u["id"] for u in initial["info_units"]], 2))[:15]
        for index, (source, target) in enumerate(pairs):
            self.case.relations.append(Relation(id=f"bounded-{index}", source_id=source, target_id=target,
                relation_type="support", evidence_unit_ids=[target], rationale="本议题的补充关系", certainty="explicit"))
        case_store.save_case(self.case, set_current=False)
        projected = investigation_context(self.case, self.issue, "入住实际计租", selected_info_unit_ids=[new_ids[-1]],
                                          focused_object_id=self.visible)["canvas"]
        self.assertEqual([u["id"] for u in projected["info_units"][:3]], [new_ids[-1], self.visible, new_ids[0]])
        self.assertEqual(len(projected["info_units"]), 8)
        self.assertTrue(projected["has_more_info_units"])
        self.assertEqual(len(projected["relations"]), 12)
        self.assertTrue(projected["has_more_relations"])
        request = self.actual_context(goal="入住实际计租", selected=[new_ids[-1]], focus=self.visible)
        self.assertEqual(len(self.ids(request)), 8)
        self.assertTrue({new_ids[-1], self.visible, new_ids[0]} <= set(self.ids(request)))
        self.assertNotIn(self.hidden, self.ids(request))
        self.assertTrue(all({r["source_id"], r["target_id"]} <= set(self.ids(request))
                            for r in request["materials"]["relations"]["items"]))
        self.assertTrue(any(n.get("tool") == "initial_canvas" for n in request["notices"]))
        self.assert_canvases_unchanged()

    def test_baseline_write_failure_aborts_case_commit_and_retry_succeeds(self):
        fid, quote = self.append()
        before = self.path.read_bytes()
        raw = model_result([model_unit(fid, quote, content="待重试的新记录。")])
        with patch.object(issue_workspaces, "write_json", side_effect=OSError("workspace disk full")):
            response, _ = self.run_with_reply(self.case_dict, raw)
        self.assertEqual(response.status_code, 500, response.text)
        self.assertEqual(self.path.read_bytes(), before)
        stored = case_store.load_case(self.case.case_id)
        self.assertEqual(len(stored.info_units), 3)
        self.assertEqual(extraction_service.pending_extraction_files(stored), [fid])
        self.assertEqual(set(self.ids(self.actual_context())), {self.visible, self.hidden, self.foreign})
        self.extract([model_unit(fid, quote, content="待重试的新记录。")])
        self.assertEqual(set(self.ids(self.actual_context())), {self.visible, self.case.info_units[-1].id})
        self.assert_canvases_unchanged()

    def test_case_commit_failure_leaves_safe_baseline_and_retry_adds_new_scope(self):
        fid, quote = self.append()
        original = extraction_service.save_case
        def save(candidate, **kwargs):
            if candidate.stage_runs[-1].status.value == "success":
                raise OSError("case commit failed")
            return original(candidate, **kwargs)
        with patch.object(extraction_service, "save_case", side_effect=save):
            response, _ = self.run_with_reply(self.case_dict, model_result([model_unit(fid, quote, content="提交重试记录。")]))
        self.assertEqual(response.status_code, 500, response.text)
        self.assertEqual(set(self.ids(self.actual_context())), {self.visible, self.hidden, self.foreign})
        self.extract([model_unit(fid, quote, content="提交重试记录。")])
        self.assertEqual(set(self.ids(self.actual_context())), {self.visible, self.case.info_units[-1].id})
        self.assert_canvases_unchanged()

    def test_committed_case_is_readable_without_post_commit_workspace_write_and_sync_recovers(self):
        fid, quote = self.append()
        self.extract([model_unit(fid, quote, content="持久化后的补充记录。")])
        new = self.case.info_units[-1].id
        before = self.path.read_bytes()
        with patch.object(issue_workspaces, "write_json", side_effect=OSError("workspace read only")):
            self.assertEqual(set(self.ids(self.actual_context())), {self.visible, new})
            with self.assertRaises(OSError):
                persist_issue_workspaces(self.case, get_issue_workspaces(self.case))
        self.assertEqual(self.path.read_bytes(), before)
        persist_issue_workspaces(self.case, get_issue_workspaces(self.case))
        state = get_issue_workspaces(self.case)
        self.assertEqual(state.workspaces[self.issue].investigation_info_unit_ids, [self.visible, new])
        persist_issue_workspaces(self.case, state)
        self.assertEqual(set(self.ids(self.actual_context())), {self.visible, new})
        self.assert_canvases_unchanged()
