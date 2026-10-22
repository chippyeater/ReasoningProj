import json
from unittest.mock import AsyncMock, patch

from support import IsolatedCaseTest, quote_id_for
from test_v02_extraction import model_result, model_unit
from app import case_store, llm_client, observability, relation_service


CONFIG = llm_client.LLMConfig(
    "azure", "https://test.services.ai.azure.com/openai/v1", "configured-model", "test-secret",
)


def relation_result(source_id, target_id, relation_type="about", evidence_ids=None,
                    rationale="两节点存在直接语义关系", certainty="explicit"):
    return json.dumps({"relations": [{
        "id": "model-temp-1",
        "source_id": source_id,
        "target_id": target_id,
        "relation_type": relation_type,
        "evidence_unit_ids": evidence_ids or [source_id],
        "rationale": rationale,
        "certainty": certainty,
    }]}, ensure_ascii=False)


class TestRelationAPI(IsolatedCaseTest):
    def setUp(self):
        super().setUp()
        self.case = self.upload(raw="甲方主体\n付款行为已发生\n双方约定每月租金三千元\n乙方否认付款行为".encode())
        self.case_id = self.case["case_id"]
        self.units = [
            self._create("entity", "person", "甲方主体", "甲方为合同当事人", "甲方主体"),
            self._create("event", "documented", "付款行为", "材料记载付款行为已经发生", "付款行为已发生"),
            self._create("rule", None, "租金约定", "双方约定每月租金为三千元", "双方约定每月租金三千元"),
            self._create("position", None, "否认付款", "乙方否认付款行为已经发生", "乙方否认付款行为", speaker="乙方"),
        ]

    def _create(self, unit_type, subtype, title, content, quote, speaker=None):
        payload = {
            "type": unit_type, "subtype": subtype, "title": title, "content": content,
            "evidence_ids": [quote_id_for(self.case_id, self.case["files"][0]["file_id"], quote)],
            "speaker": speaker, "side": None, "actor": None, "target": None, "related_issue": None,
        }
        response = self.client.post(f"/api/cases/{self.case_id}/info-units", json=payload)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["case"]["info_units"][-1]

    def _run(self, content, selected=None, side_effect=None, workflow_id=None):
        reply = llm_client.LLMReply(content, {"mock": True})
        with patch.object(llm_client, "load_config", return_value=CONFIG), patch.object(
                llm_client, "call_structured", new=AsyncMock(side_effect=side_effect, return_value=reply)) as call:
            payload = {"case_id": self.case_id}
            if selected is not None:
                payload["selected_info_unit_ids"] = selected
            if workflow_id is not None:
                payload["workflow_id"] = workflow_id
            response = self.client.post("/api/relation/run", json=payload)
        return response, call

    def test_full_generation_appends_with_server_id_and_audit(self):
        source, target = self.units[1], self.units[2]
        response, call = self._run(relation_result(source["id"], target["id"], "support"))
        self.assertEqual(response.status_code, 200, response.text)
        added = response.json()["added_relations"]
        self.assertEqual(len(added), 1)
        self.assertTrue(added[0]["id"].startswith("relation-"))
        self.assertNotEqual(added[0]["id"], "model-temp-1")
        model_input = call.await_args.args[2]
        self.assertEqual(model_input["scope"], "all")
        self.assertEqual(len(model_input["info_units"]), 4)
        stored = case_store.load_case(self.case_id)
        self.assertEqual(stored.relations[0].id, added[0]["id"])
        self.assertEqual(stored.stage_runs[-1].stage.value, "relation")
        self.assertEqual(stored.stage_runs[-1].status.value, "success")

    def test_full_generation_with_fewer_than_two_units_returns_empty_without_model(self):
        for unit in self.units[1:]:
            response = self.client.delete(f"/api/cases/{self.case_id}/info-units/{unit['id']}")
            self.assertEqual(response.status_code, 200, response.text)
        with patch.object(llm_client, "call_structured", new=AsyncMock()) as call:
            response = self.client.post("/api/relation/run", json={"case_id": self.case_id})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"added_relations": []})
        call.assert_not_awaited()

    def test_selected_generation_is_scoped_and_repeat_is_idempotent(self):
        selected = [self.units[1]["id"], self.units[3]["id"]]
        raw = relation_result(selected[0], selected[1], "contradict", selected)
        first, call = self._run(raw, selected)
        self.assertEqual(first.status_code, 200, first.text)
        first_relation = first.json()["added_relations"][0]
        self.assertEqual({item["id"] for item in call.await_args.args[2]["info_units"]}, set(selected))
        self.assertEqual(call.await_args.args[2]["scope"], "selected")

        reverse = relation_result(selected[1], selected[0], "contradict", list(reversed(selected)))
        second, call = self._run(reverse, selected)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(second.json()["added_relations"], [])
        stored = case_store.load_case(self.case_id)
        self.assertEqual(len(stored.relations), 1)
        self.assertEqual(stored.relations[0].id, first_relation["id"])
        self.assertEqual(call.await_args.args[2]["existing_relations"][0]["id"], first_relation["id"])

    def test_scope_and_generated_references_are_enforced_atomically(self):
        selected = [self.units[1]["id"], self.units[2]["id"]]
        outside = self.units[3]["id"]
        before = case_store.load_case(self.case_id).model_dump(mode="json")
        response, _ = self._run(relation_result(selected[0], outside, "support"), selected)
        self.assertEqual(response.status_code, 422, response.text)
        after = case_store.load_case(self.case_id)
        self.assertEqual(after.relations, [])
        self.assertEqual([u.model_dump(mode="json") for u in after.info_units], before["info_units"])

        bad_scope, call = self._run('{"relations": []}', [selected[0], "missing"])
        self.assertEqual(bad_scope.status_code, 422, bad_scope.text)
        call.assert_not_awaited()

    def test_concurrent_change_rejects_stale_result(self):
        source, target = self.units[1], self.units[2]
        raw = relation_result(source["id"], target["id"], "support")

        async def change_case(*_args):
            changed = case_store.load_case(self.case_id)
            changed.case_title = "并发更新后的标题"
            case_store.save_case(changed, set_current=False)
            return llm_client.LLMReply(raw, {"mock": True})

        response, _ = self._run(raw, side_effect=change_case)
        self.assertEqual(response.status_code, 409, response.text)
        stored = case_store.load_case(self.case_id)
        self.assertEqual(stored.case_title, "并发更新后的标题")
        self.assertEqual(stored.relations, [])

    def test_relation_failure_preserves_info_units(self):
        before = [unit.model_dump(mode="json") for unit in case_store.load_case(self.case_id).info_units]
        failure = llm_client.LLMFailure("llm_call_failed", "mock relation failure")
        response, _ = self._run("", side_effect=failure)
        self.assertEqual(response.status_code, 502, response.text)
        stored = case_store.load_case(self.case_id)
        self.assertEqual([unit.model_dump(mode="json") for unit in stored.info_units], before)
        self.assertEqual(stored.relations, [])

    def test_observability_outage_does_not_break_relation_or_local_audit(self):
        settings = observability.ObservabilitySettings(
            enabled=True, capture_content=False, public_key="pk-test", secret_key="sk-test",
            base_url="https://cloud.langfuse.com", environment="test",
        )
        source, target = self.units[1], self.units[2]
        with patch.object(observability, "load_settings", return_value=settings), patch.object(
                observability, "_Backend", side_effect=RuntimeError("mock telemetry outage")):
            response, _ = self._run(relation_result(source["id"], target["id"], "support"))
        self.assertEqual(response.status_code, 200, response.text)
        stored = case_store.load_case(self.case_id)
        run = stored.stage_runs[-1]
        audit = json.loads((self.data_dir / self.case_id / run.output_snapshot_path).read_text(
            encoding="utf-8"))
        self.assertEqual(audit["status"], "success")
        self.assertEqual(len(stored.relations), 1)

    def test_successful_extraction_then_relation_failure_keeps_extracted_units(self):
        workflow_id = "workflow-cross-stage-test"
        file_id = self.case["files"][0]["file_id"]
        evidence_id = quote_id_for(self.case_id, file_id, "付款行为已发生")
        draft = model_result([
            model_unit(file_id, evidence_id),
            model_unit(file_id, evidence_id, title="第二付款记录", content="材料另行记载付款行为仍有争议。"),
        ])
        review = json.dumps({
            "decisions": [
                {"draft_index": index, "action": "keep", "finding": "保留独立命题。", "final_indices": [index]}
                for index in range(2)
            ],
            "coverage_findings": [], "added_final_indices": [], "extraction": json.loads(draft),
        }, ensure_ascii=False)
        with patch.object(llm_client, "load_config", return_value=CONFIG), patch.object(
                llm_client, "call_structured", new=AsyncMock(side_effect=[
                    llm_client.LLMReply(draft, {"mock": "extraction"}),
                    llm_client.LLMReply(review, {"mock": "consolidation"}),
                ])):
            extracted = self.client.post("/api/extraction/run", json={
                "case_id": self.case_id, "workflow_id": workflow_id,
            })
        self.assertEqual(extracted.status_code, 200, extracted.text)

        failure = llm_client.LLMFailure("llm_call_failed", "mock relation failure")
        response, _ = self._run("", side_effect=failure, workflow_id=workflow_id)
        self.assertEqual(response.status_code, 502, response.text)
        stored = case_store.load_case(self.case_id)
        self.assertEqual(
            [unit.model_dump(mode="json") for unit in stored.info_units],
            extracted.json()["info_units"],
        )
        self.assertEqual(stored.relations, [])
        stage_runs = stored.stage_runs[-2:]
        self.assertEqual([run.stage.value for run in stage_runs], ["extraction", "relation"])
        audits = [json.loads((self.data_dir / self.case_id / run.output_snapshot_path).read_text(
            encoding="utf-8")) for run in stage_runs]
        self.assertEqual([audit["workflow_id"] for audit in audits], [workflow_id, workflow_id])
        self.assertIn("Extraction", audits[0]["run_name"])
        self.assertIn("Relation", audits[1]["run_name"])

    def test_manual_crud_for_all_types_and_persists(self):
        ids = [unit["id"] for unit in self.units]
        created_ids = []
        for source, target, relation_type in (
            (ids[0], ids[1], "about"),
            (ids[2], ids[1], "support"),
            (ids[1], ids[3], "contradict"),
        ):
            response = self.client.post(f"/api/cases/{self.case_id}/relations", json={
                "source_id": source, "target_id": target, "relation_type": relation_type,
                "evidence_unit_ids": [source], "rationale": f"{relation_type} 的明确依据",
                "certainty": "explicit",
            })
            self.assertEqual(response.status_code, 201, response.text)
            created_ids.append(response.json()["case"]["relations"][-1]["id"])

        relation_id = created_ids[0]
        fetched = self.client.get(f"/api/cases/{self.case_id}")
        self.assertEqual(fetched.status_code, 200, fetched.text)
        self.assertIn(relation_id, {item["id"] for item in fetched.json()["case"]["relations"]})
        edited = self.client.patch(f"/api/cases/{self.case_id}/relations/{relation_id}", json={
            "rationale": "更新后的关系理由", "certainty": "inferred",
        })
        self.assertEqual(edited.status_code, 200, edited.text)
        updated = next(item for item in edited.json()["case"]["relations"] if item["id"] == relation_id)
        self.assertEqual(updated["id"], relation_id)
        self.assertEqual(updated["rationale"], "更新后的关系理由")
        self.assertEqual(updated["certainty"], "inferred")

        deleted = self.client.delete(f"/api/cases/{self.case_id}/relations/{created_ids[1]}")
        self.assertEqual(deleted.status_code, 200, deleted.text)
        reopened = self.client.get(f"/api/cases/{self.case_id}").json()["case"]
        self.assertEqual({item["id"] for item in reopened["relations"]}, {created_ids[0], created_ids[2]})
        self.assertEqual(len(reopened["info_units"]), 4)

    def test_manual_invalid_duplicate_and_save_failure_do_not_mutate(self):
        ids = [unit["id"] for unit in self.units]
        payload = {
            "source_id": ids[1], "target_id": ids[3], "relation_type": "contradict",
            "evidence_unit_ids": [ids[1]], "rationale": "付款陈述相互冲突", "certainty": "explicit",
        }
        created = self.client.post(f"/api/cases/{self.case_id}/relations", json=payload)
        self.assertEqual(created.status_code, 201, created.text)
        stable = (self.data_dir / self.case_id / "case.json").read_bytes()

        cases = [
            payload,
            payload | {"source_id": ids[3], "target_id": ids[1]},
            payload | {"target_id": "missing"},
            payload | {"target_id": ids[2], "relation_type": "about", "evidence_unit_ids": ["missing"]},
            payload | {"source_id": ids[0], "target_id": ids[3], "relation_type": "contradict"},
        ]
        expected = [409, 409, 422, 422, 422]
        for candidate, status in zip(cases, expected):
            response = self.client.post(f"/api/cases/{self.case_id}/relations", json=candidate)
            self.assertEqual(response.status_code, status, response.text)
            self.assertEqual((self.data_dir / self.case_id / "case.json").read_bytes(), stable)

        with patch.object(relation_service, "save_case", side_effect=OSError("denied")):
            response = self.client.patch(
                f"/api/cases/{self.case_id}/relations/{created.json()['case']['relations'][0]['id']}",
                json={"rationale": "不应保存的修改"},
            )
        self.assertEqual(response.status_code, 500, response.text)
        self.assertEqual((self.data_dir / self.case_id / "case.json").read_bytes(), stable)
