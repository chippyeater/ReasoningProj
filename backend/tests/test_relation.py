import json
from contextlib import nullcontext
from unittest.mock import AsyncMock, MagicMock, patch

from support import IsolatedCaseTest, model_result, model_unit, quote_id_for
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

    def _run(self, content, selected=None, side_effect=None, workflow_id=None,
             repair_content=None):
        reply = llm_client.LLMReply(content, {"mock": True})
        if repair_content is not None:
            side_effect = [reply, llm_client.LLMReply(repair_content, {"mock": "repair"})]
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
        self.assertEqual(response.json()["status"], "success")
        self.assertEqual(response.json()["discarded_count"], 0)
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
        audit = json.loads((self.data_dir / self.case_id /
                            stored.stage_runs[-1].output_snapshot_path).read_text(encoding="utf-8"))
        self.assertEqual(audit["model_calls"][0]["prompt_name"], "relation")
        self.assertRegex(audit["model_calls"][0]["prompt_version"], r"^v[1-9][0-9]*$")
        self.assertRegex(audit["model_calls"][0]["prompt_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(audit["model_calls"][0]["schema_version"], "0.3")
        self.assertEqual(audit["model_calls"][0]["model"], "configured-model")

    def test_full_generation_with_fewer_than_two_units_returns_empty_without_model(self):
        for unit in self.units[1:]:
            response = self.client.delete(f"/api/cases/{self.case_id}/info-units/{unit['id']}")
            self.assertEqual(response.status_code, 200, response.text)
        with patch.object(llm_client, "call_structured", new=AsyncMock()) as call:
            response = self.client.post("/api/relation/run", json={"case_id": self.case_id})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {
            "status": "empty", "added_relations": [], "discarded_count": 0,
        })
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
        second, call = self._run(
            reverse, selected,
            repair_content=json.dumps({
                "repairs": [], "discarded_ids": ["model-temp-1"],
            }, ensure_ascii=False))
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(second.json()["status"], "partial_success")
        self.assertEqual(second.json()["added_relations"], [])
        stored = case_store.load_case(self.case_id)
        self.assertEqual(len(stored.relations), 1)
        self.assertEqual(stored.relations[0].id, first_relation["id"])
        self.assertEqual(call.await_args.args[2]["existing_relations"][0]["id"], first_relation["id"])

    def test_scope_and_generated_references_are_enforced_atomically(self):
        selected = [self.units[1]["id"], self.units[2]["id"]]
        outside = self.units[3]["id"]
        before = case_store.load_case(self.case_id).model_dump(mode="json")
        response, call = self._run(
            relation_result(selected[0], outside, "support"), selected,
            repair_content=json.dumps({
                "repairs": [], "discarded_ids": ["model-temp-1"],
            }, ensure_ascii=False))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "partial_success")
        self.assertEqual(call.await_count, 2)
        after = case_store.load_case(self.case_id)
        self.assertEqual(after.relations, [])
        self.assertEqual([u.model_dump(mode="json") for u in after.info_units], before["info_units"])

        bad_scope, call = self._run('{"relations": []}', [selected[0], "missing"])
        self.assertEqual(bad_scope.status_code, 422, bad_scope.text)
        call.assert_not_awaited()

    def test_illegal_endpoint_is_revalidated_once_while_valid_relation_is_preserved(self):
        valid = json.loads(relation_result(
            self.units[1]["id"], self.units[2]["id"], "support"))["relations"][0]
        invalid = json.loads(relation_result(
            self.units[1]["id"], self.units[0]["id"], "support"))["relations"][0]
        invalid["id"] = "trace-invalid-endpoint"
        initial = json.dumps({"relations": [valid, invalid]}, ensure_ascii=False)
        # The single repair call returns a still-invalid replacement. Full
        # revalidation must discard it without touching the valid candidate.
        repair = json.dumps({
            "repairs": [invalid], "discarded_ids": [],
        }, ensure_ascii=False)
        response, call = self._run(initial, repair_content=repair)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "partial_success")
        self.assertEqual(response.json()["discarded_count"], 1)
        self.assertEqual(len(response.json()["added_relations"]), 1)
        self.assertEqual(call.await_count, 2)
        stored = case_store.load_case(self.case_id)
        self.assertEqual(len(stored.relations), 1)
        audit = json.loads((self.data_dir / self.case_id /
                            stored.stage_runs[-1].output_snapshot_path).read_text(encoding="utf-8"))
        endpoint_error = next(
            error for error in audit["initial_validation"]["errors"]
            if error["item_id"] == invalid["id"])
        self.assertEqual(endpoint_error, {
            "item_id": invalid["id"], "field_path": "target_id",
            "error_code": "illegal_relation_endpoint",
            "message": "support target must not be an entity",
        })
        repair_input = json.loads((self.data_dir / self.case_id /
                                   audit["repair"]["input_path"]).read_text(encoding="utf-8"))
        self.assertEqual([item["original"]["id"] for item in repair_input["failed_relations"]],
                         [invalid["id"]])
        self.assertEqual(audit["revalidation"]["failed_ids"], [invalid["id"]])

    def test_invalid_relation_can_be_repaired_without_changing_accepted_candidates(self):
        accepted = json.loads(relation_result(
            self.units[3]["id"], self.units[1]["id"], "contradict"))["relations"][0]
        accepted["id"] = "accepted-original"
        invalid = json.loads(relation_result(
            self.units[1]["id"], self.units[0]["id"], "support"))["relations"][0]
        invalid["id"] = "repair-me"
        repaired = dict(invalid)
        repaired["target_id"] = self.units[2]["id"]
        backend = MagicMock()
        backend.propagate_attributes.return_value = nullcontext()
        span_starts = []

        def start_observation(**kwargs):
            span_starts.append(kwargs)
            return nullcontext(MagicMock())

        backend.client.start_as_current_observation.side_effect = start_observation
        settings = observability.ObservabilitySettings(
            enabled=True, capture_content=True, public_key="pk-test", secret_key="sk-test",
            base_url="https://cloud.langfuse.com", environment="test")
        with patch.object(observability, "load_settings", return_value=settings), patch.object(
                observability, "_get_backend", return_value=backend):
            response, call = self._run(
                json.dumps({"relations": [accepted, invalid]}, ensure_ascii=False),
                repair_content=json.dumps({
                    "repairs": [repaired], "discarded_ids": [],
                }, ensure_ascii=False))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(call.await_count, 2)
        self.assertEqual(response.json()["status"], "success")
        self.assertEqual(len(response.json()["added_relations"]), 2)
        stored = case_store.load_case(self.case_id)
        audit = json.loads((self.data_dir / self.case_id /
                            stored.stage_runs[-1].output_snapshot_path).read_text(encoding="utf-8"))
        self.assertFalse(any(
            change["item_id"] == accepted["id"] for change in audit["repair"]["changes"]))
        self.assertEqual(audit["revalidation"]["failed_ids"], [])
        self.assertEqual([record["prompt_name"] for record in audit["model_calls"]], [
            "relation", "relation_validation_repair",
        ])
        names = [start["name"] for start in span_starts]
        self.assertIn("Relation Validation Repair · model call", names)
        self.assertIn("Relation Validation Repair · resolution", names)
        self.assertIn("Relation Validation Repair · revalidate", names)
        repair_generation = next(
            start for start in span_starts
            if start["name"] == "Relation Validation Repair · model call")
        self.assertEqual(repair_generation["version"], "v1")
        self.assertEqual(
            repair_generation["metadata"]["prompt_name"], "relation_validation_repair")

    def test_relation_repair_transport_failure_does_not_save_initially_valid_relation(self):
        valid = json.loads(relation_result(
            self.units[1]["id"], self.units[2]["id"], "support"))["relations"][0]
        invalid = json.loads(relation_result(
            self.units[1]["id"], self.units[0]["id"], "support"))["relations"][0]
        invalid["id"] = "repair-outage"
        initial = json.dumps({"relations": [valid, invalid]}, ensure_ascii=False)
        side_effect = [
            llm_client.LLMReply(initial, {"mock": "initial"}),
            llm_client.LLMFailure("llm_call_failed", "mock repair outage"),
        ]
        response, call = self._run(initial, side_effect=side_effect)
        self.assertEqual(response.status_code, 502, response.text)
        self.assertEqual(call.await_count, 2)
        self.assertEqual(case_store.load_case(self.case_id).relations, [])

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

    def test_generated_relation_save_failure_does_not_partially_persist(self):
        before = (self.data_dir / self.case_id / "case.json").read_bytes()
        source, target = self.units[1], self.units[2]
        with patch.object(relation_service, "save_case", side_effect=OSError("denied")):
            response, call = self._run(relation_result(source["id"], target["id"], "support"))
        self.assertEqual(response.status_code, 500, response.text)
        self.assertEqual(response.json()["detail"]["code"], "relation_audit_persistence_failed")
        self.assertEqual(call.await_count, 1)
        self.assertEqual((self.data_dir / self.case_id / "case.json").read_bytes(), before)

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
        for unit in reversed(self.units):
            response = self.client.delete(f"/api/cases/{self.case_id}/info-units/{unit['id']}")
            self.assertEqual(response.status_code, 200, response.text)
        file_id = self.case["files"][0]["file_id"]
        evidence_id = quote_id_for(self.case_id, file_id, "付款行为已发生")
        draft = model_result([
            model_unit(file_id, evidence_id),
            model_unit(file_id, evidence_id, title="第二付款记录", content="材料另行记载付款行为仍有争议。"),
        ])
        with patch.object(llm_client, "load_config", return_value=CONFIG), patch.object(
                llm_client, "call_structured", new=AsyncMock(return_value=
                    llm_client.LLMReply(draft, {"mock": "extraction"}))):
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
