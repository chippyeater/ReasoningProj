"""Committed file scopes, conservative append/merge, and failure atomicity (no paid calls)."""
import json
from unittest.mock import AsyncMock, patch

from support import IsolatedCaseTest, model_result, model_unit, quote_id_for
import test_extraction_api as api_fixture
from app import case_store, extraction_service, llm_client
from app.extraction import deduplicate_units
from app.errors import AppError
from app.schemas import Relation
from scripts.baseline_extraction import baseline_extraction


class TestIncrementalExtraction(IsolatedCaseTest):
    run_with_reply = api_fixture.TestExtractionAPI.run_with_reply

    def initial(self):
        case = self.upload(raw="甲称付款".encode("utf-8"), source_kind="testimony")
        self.extract(case, case["files"][0], "甲称付款")
        return case_store.load_case(case["case_id"])

    def append(self, case, text, name="new.txt"):
        cid = case.case_id
        response = self.client.post(f"/api/cases/{cid}/files", data={
            "source_kinds": json.dumps({name: "testimony"})},
            files={"files": (name, text.encode("utf-8"))})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["case"]

    def extract(self, case, file, text, **changes):
        raw = model_result([model_unit(file["file_id"], quote_id_for(
            case["case_id"], file["file_id"], text), **changes)])
        response, call = self.run_with_reply(case, raw)
        self.assertEqual(response.status_code, 200, response.text)
        return response, call

    def business(self, case):
        return case.model_dump(mode="json", exclude={"stage_runs", "updated_at"})

    def test_two_new_batches_only_send_their_files_and_keep_old_edits(self):
        old = self.initial()
        old.info_units[0].title = "用户编辑的标题"
        old.info_units[0].content = "用户修订：甲自称付款。"
        case_store.save_case(old)
        for index in (1, 2):
            case = self.append(old, f"新记录{index}", f"batch{index}.txt")
            response, call = self.extract(case, case["files"][-1], f"新记录{index}", content=f"独立记录{index}")
            selected = [file["file_id"] for file in call.call_args.args[2]["files"]]
            self.assertEqual(selected, [case["files"][-1]["file_id"]])
            stored = case_store.load_case(old.case_id)
            self.assertEqual(stored.info_units[0], old.info_units[0])
            self.assertEqual(len(stored.info_units), index + 1)
            self.assertEqual(stored.core_issue, old.core_issue)
            run = stored.stage_runs[-1]
            for path in (run.input_snapshot_path, run.output_snapshot_path):
                snapshot = json.loads((self.data_dir / old.case_id / path).read_text(encoding="utf-8"))
                self.assertEqual(snapshot["selected_file_ids"], selected)
            self.assertEqual(response.json()["info_units"], [u.model_dump(mode="json") for u in stored.info_units])
            old = stored

    def test_matching_old_node_only_merges_unique_sources_preserves_references(self):
        old = self.initial()
        old.info_units[0].title = "用户保留标题"
        uid = old.info_units[0].id
        other = old.info_units[0].model_copy(deep=True, update={"id": "other", "content": "其他独立信息"})
        old.info_units.append(other)
        old.relations.append(Relation(id="r1", source_id=uid, target_id="other", relation_type="about",
                                      evidence_unit_ids=[uid], rationale="既有关系", certainty="explicit"))
        case_store.save_case(old)
        issue = old.core_issue
        def edit(**values):
            result = self.client.patch(f"/api/cases/{old.case_id}/issue-reasoning", json={"issue": issue, **values})
            self.assertEqual(result.status_code, 200, result.text)
            return result.json()["object_id"]
        hid = edit(hypothesis={"content": "既有假设"})
        edit(gap={"content": "尚待核对", "hypothesis_ids": [hid], "basis_info_unit_ids": [uid]})
        edit(link={"hypothesis_id": hid, "info_unit_id": uid, "meaning": "support", "rationale": "原有支持依据"})
        path = self.data_dir / old.case_id / "issue_workspaces.json"
        workspace = path.read_bytes()
        case = self.append(old, "甲再次称付款")
        fid = case["files"][-1]["file_id"]
        quote = quote_id_for(old.case_id, fid, "甲再次称付款")
        response, _ = self.run_with_reply(case, model_result([model_unit(fid, quote), model_unit(fid, quote)]))
        self.assertEqual(response.status_code, 200, response.text)
        stored = case_store.load_case(old.case_id)
        before = old.info_units[0].model_dump(exclude={"source_refs"})
        self.assertEqual(stored.info_units[0].model_dump(exclude={"source_refs"}), before)
        self.assertEqual(len(stored.info_units[0].source_refs), 2)
        self.assertEqual(stored.info_units[1], other)
        self.assertEqual(stored.relations, old.relations)
        self.assertEqual(path.read_bytes(), workspace)
        loaded = self.client.get(f"/api/cases/{old.case_id}/issue-workspaces")
        self.assertEqual(loaded.status_code, 200, loaded.text)

    def test_existing_duplicate_entity_maps_actor_target_and_preserves_ambiguous_saved_nodes(self):
        case = self.upload(raw="甲为付款人".encode("utf-8"), source_kind="testimony")
        file = case["files"][0]
        entity = dict(type="entity", subtype="person", title="甲方", content="甲为付款人。",
                      speaker=None, side=None, related_issue=None)
        self.extract(case, file, "甲为付款人", **entity)
        old = case_store.load_case(case["case_id"])
        eid = old.info_units[0].id
        case = self.append(old, "甲再次出现\n甲称付款")
        fid = case["files"][-1]["file_id"]
        a = model_unit(fid, quote_id_for(old.case_id, fid, "甲再次出现"), local_id="entity-a", **entity)
        b = {**a, "local_id": "entity-b"}
        event = model_unit(fid, quote_id_for(old.case_id, fid, "甲称付款"),
                           actor_local_id="entity-b", target_local_id="entity-a")
        response, _ = self.run_with_reply(case, model_result([a, b, event]))
        self.assertEqual(response.status_code, 200, response.text)
        result = case_store.load_case(old.case_id)
        self.assertEqual(len(result.info_units), 2)
        self.assertEqual((result.info_units[1].actor, result.info_units[1].target), (eid, eid))
        self.assertEqual(len(result.info_units[0].source_refs), 2)
        # Saved duplicates remain independent; an ambiguous new match is kept separately.
        duplicate = result.info_units[0].model_copy(deep=True, update={"id": "saved-duplicate"})
        result.info_units.append(duplicate)
        new = duplicate.model_copy(deep=True, update={"id": "incoming"})
        merged, _ = deduplicate_units([new], result.info_units)
        self.assertEqual([u.id for u in merged], [u.id for u in result.info_units] + ["incoming"])
        self.assertEqual(merged[:3], result.info_units)

    def test_core_and_issue_workspaces_are_preserved_new_labels_append(self):
        old = self.initial()
        case = self.append(old, "新争点材料")
        fid = case["files"][-1]["file_id"]
        raw = json.loads(model_result([model_unit(fid, quote_id_for(old.case_id, fid, "新争点材料"),
                                                 related_issue="新的子争点")]))
        raw.update(core_issue="新的子争点", issue_set=["新的子争点"])
        response, _ = self.run_with_reply(case, json.dumps(raw, ensure_ascii=False))
        self.assertEqual(response.status_code, 200, response.text)
        stored = case_store.load_case(old.case_id)
        self.assertEqual(stored.core_issue, old.core_issue)
        self.assertEqual(stored.issue_set, [*old.issue_set, "新的子争点"])

    def test_incremental_failures_leave_business_data_and_file_retryable(self):
        old = self.initial()
        case = self.append(old, "可重试材料")
        fid = case["files"][-1]["file_id"]
        raw = model_result([model_unit(fid, quote_id_for(old.case_id, fid, "可重试材料"), content="新的信息")])
        for failure in ("model", "validation", "audit", "save"):
            with self.subTest(failure=failure):
                before = case_store.load_case(old.case_id)
                reply = llm_client.LLMReply(raw, {})
                with patch.object(llm_client, "load_config", return_value=api_fixture.CONFIG), patch.object(
                        llm_client, "call_structured", new=AsyncMock(
                            side_effect=llm_client.LLMFailure("llm_call_failed", "mock failure") if failure == "model" else None,
                            return_value=llm_client.LLMReply("invalid JSON", {}) if failure == "validation" else reply)):
                    target = "_write_audit" if failure == "audit" else "save_case"
                    original = getattr(extraction_service, target)
                    with patch.object(extraction_service, target,
                                      side_effect=PermissionError("mock storage failure") if failure in {"audit", "save"} else original):
                        response = self.client.post("/api/extraction/run", json={"case_id": old.case_id})
                self.assertGreaterEqual(response.status_code, 400, response.text)
                after = case_store.load_case(old.case_id)
                self.assertEqual(self.business(after), self.business(before))
                self.assertEqual(extraction_service.pending_extraction_files(after), [fid])
        self.extract(case, case["files"][-1], "可重试材料", content="新的信息")
        self.assertEqual(extraction_service.pending_extraction_files(case_store.load_case(old.case_id)), [])

    def test_uncommitted_success_audit_does_not_mark_file_processed(self):
        case = self.upload(raw="材料".encode("utf-8"))
        fid = case["files"][0]["file_id"]
        original = extraction_service.save_case
        def fail_success(candidate, **kwargs):
            if candidate.stage_runs[-1].status.value == "success":
                raise OSError("commit failed")
            return original(candidate, **kwargs)
        with patch.object(extraction_service, "save_case", side_effect=fail_success):
            response, _ = self.run_with_reply(case, model_result([model_unit(fid, quote_id_for(case["case_id"], fid, "材料"))]))
        self.assertEqual(response.status_code, 500)
        stored = case_store.load_case(case["case_id"])
        self.assertEqual(stored.info_units, [])
        self.assertEqual(extraction_service.pending_extraction_files(stored), [fid])

    def test_model_cannot_read_old_file_via_repair_and_failed_file_can_retry(self):
        old = self.initial()
        case = self.append(old, "本批新材料")
        fid = case["files"][-1]["file_id"]
        invalid = model_unit(fid, quote_id_for(old.case_id, old.files[0].file_id, "甲称付款"))
        replies = [llm_client.LLMReply(model_result([invalid]), {}), llm_client.LLMReply(json.dumps({
            "repairs": [], "unresolved_ids": [invalid["local_id"]]}), {})]
        with patch.object(llm_client, "load_config", return_value=api_fixture.CONFIG), patch.object(
                llm_client, "call_structured", new=AsyncMock(side_effect=replies)) as call:
            response = self.client.post("/api/extraction/run", json={"case_id": old.case_id})
        self.assertEqual(response.status_code, 422, response.text)
        repair = call.call_args_list[1].args[2]
        self.assertTrue(all(item["file_id"] == fid for item in repair["evidence_candidates"]))
        stored = case_store.load_case(old.case_id)
        self.assertEqual(stored.info_units, old.info_units)
        self.assertEqual(extraction_service.pending_extraction_files(stored), [fid])

    def test_concurrent_change_during_success_audit_is_not_overwritten(self):
        old = self.initial()
        case = self.append(old, "本批材料")
        fid = case["files"][-1]["file_id"]
        original = extraction_service._write_audit
        def audit_and_edit(case_id, run, audit):
            original(case_id, run, audit)
            if run.status.value == "success":
                changed = case_store.load_case(case_id)
                changed.case_title = "并发保留的用户标题"
                case_store.save_case(changed, set_current=False)
        with patch.object(extraction_service, "_write_audit", side_effect=audit_and_edit):
            response, _ = self.run_with_reply(case, model_result([model_unit(
                fid, quote_id_for(old.case_id, fid, "本批材料"), content="独立新信息")]))
        self.assertEqual(response.status_code, 409, response.text)
        stored = case_store.load_case(old.case_id)
        self.assertEqual(stored.case_title, "并发保留的用户标题")
        self.assertEqual(stored.info_units, old.info_units)
        self.assertEqual(extraction_service.pending_extraction_files(stored), [fid])

    def test_partial_success_covers_all_selected_files_and_excludes_unparsed(self):
        old = self.initial()
        case = self.append(old, "新材料")
        fid = case["files"][-1]["file_id"]
        unsupported = self.client.post(f"/api/cases/{old.case_id}/files", data={
            "source_kinds": '{"scan.png":"record"}'}, files={"files": ("scan.png", b"no OCR")})
        self.assertEqual(unsupported.status_code, 200)
        valid = model_unit(fid, quote_id_for(old.case_id, fid, "新材料"), content="独立信息")
        invalid = model_unit(fid, "missing-quote", content="无效")
        replies = [llm_client.LLMReply(model_result([valid, invalid]), {}),
                   llm_client.LLMReply(json.dumps({"repairs": [], "unresolved_ids": [invalid["local_id"]]}), {})]
        with patch.object(llm_client, "load_config", return_value=api_fixture.CONFIG), patch.object(
                llm_client, "call_structured", new=AsyncMock(side_effect=replies)) as call:
            response = self.client.post("/api/extraction/run", json={"case_id": old.case_id})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([f["file_id"] for f in call.call_args_list[0].args[2]["files"]], [fid])
        # Repair candidates cannot pull old or unsupported files back into the request.
        self.assertTrue(all(item["file_id"] == fid for item in call.call_args_list[1].args[2]["evidence_candidates"]))
        stored = case_store.load_case(old.case_id)
        audit = json.loads((self.data_dir / old.case_id / stored.stage_runs[-1].output_snapshot_path).read_text(encoding="utf-8"))
        self.assertEqual(audit["result_status"], "partial_success")
        self.assertEqual(extraction_service.pending_extraction_files(stored), [])

    def test_history_baseline_is_explicit_idempotent_and_preserves_old_audits(self):
        old = self.initial()
        success = old.stage_runs[-1]
        root = self.data_dir / old.case_id
        (root / success.input_snapshot_path).unlink()  # successful old Run with unrecoverable scope
        case = self.append(old, "确实尚未提取的新文件")
        before = (root / "case.json").read_bytes()
        with patch.object(llm_client, "call_structured", new=AsyncMock()) as call:
            response = self.client.post("/api/extraction/run", json={"case_id": old.case_id})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["code"], "extraction_history_baseline_required")
        call.assert_not_awaited()
        self.assertEqual((root / "case.json").read_bytes(), before)
        ids = [old.files[0].file_id]
        audits = {p: p.read_bytes() for p in (root / "runs").glob("*.json")}
        preview = baseline_extraction(old.case_id, ids)
        self.assertFalse(preview["applied"])
        self.assertEqual((root / "case.json").read_bytes(), before)
        self.assertEqual(preview["remaining_file_ids"], [case["files"][-1]["file_id"]])
        baseline_extraction(old.case_id, ids, apply=True)
        for path, raw in audits.items():
            self.assertEqual(path.read_bytes(), raw)
        self.assertEqual((root / "case.json.before-incremental-extraction.bak").read_bytes(), before)
        self.assertFalse(baseline_extraction(old.case_id, ids, apply=True)["baseline_required"])
        self.extract(case, case["files"][-1], "确实尚未提取的新文件", content="独立新材料")

    def test_imported_case_without_runs_requires_baseline_and_empty_case_is_noop(self):
        old = self.initial()
        old.stage_runs = [run for run in old.stage_runs if run.stage.value != "extraction"]
        case_store.save_case(old)
        with self.assertRaisesRegex(AppError, "历史提取范围"):
            extraction_service.pending_extraction_files(old)
        baseline_extraction(old.case_id, [old.files[0].file_id], apply=True)
        self.assertEqual(extraction_service.pending_extraction_files(case_store.load_case(old.case_id)), [])
        empty = self.client.post("/api/cases", json={"case_title": "空案件"}).json()["case"]
        with patch.object(llm_client, "call_structured", new=AsyncMock()) as call:
            response = self.client.post("/api/extraction/run", json={"case_id": empty["case_id"]})
        self.assertEqual(response.status_code, 204)
        call.assert_not_awaited()
        self.assertEqual(case_store.load_case(empty["case_id"]).stage_runs, [])
