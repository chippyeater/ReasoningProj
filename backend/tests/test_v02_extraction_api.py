import json
import unittest
from unittest.mock import AsyncMock, patch

from support import IsolatedCaseTest, docx_bytes
from test_v02_extraction import model_result, model_unit
from app import case_store, file_parsers, llm_client
from app.case_service import now_iso
from app import extraction_service

CONFIG = llm_client.LLMConfig("azure", "https://test.services.ai.azure.com/openai/v1", "configured-model", "test-secret")


def review_result(raw, decisions=None):
    extraction = json.loads(raw)
    if decisions is None:
        decisions = [dict(draft_index=i, action="keep", finding="命题及来源保留。", final_indices=[i])
                     for i in range(len(extraction['info_units']))]
    return json.dumps(dict(decisions=decisions, coverage_findings=[], added_final_indices=[],
                           extraction=extraction), ensure_ascii=False)


class TestExtractionAPI(IsolatedCaseTest):
    def registered_case(self, filename="case.docx", raw=None):
        return self.upload(filename, docx_bytes("甲：我已付款") if raw is None else raw, source_kind="testimony")

    def run_with_reply(self, case, raw):
        count = 0
        async def reply_for_step(*args):
            nonlocal count
            count += 1
            content = raw if count == 1 else review_result(raw)
            return llm_client.LLMReply(content, {"choices": [{"message": {"content": content}, "finish_reason": "stop"}]})
        with patch.object(llm_client, "load_config", return_value=CONFIG), \
                patch.object(llm_client, "call_structured", new=AsyncMock(side_effect=reply_for_step)) as call:
            response = self.client.post("/api/extraction/run", json={"case_id": case["case_id"]})
        return response, call

    def test_full_api_pipeline_uses_txt_and_saves_output_and_audit(self):
        case = self.registered_case()
        fid = case["files"][0]["file_id"]
        raw = model_result([model_unit(fid, "甲：我已付款")])
        with patch.object(file_parsers, "parse_text", side_effect=AssertionError("must not parse DOCX again")):
            response, call = self.run_with_reply(case, raw)
        self.assertEqual(response.status_code, 200, response.text)
        output = response.json()
        self.assertEqual(set(output), {"core_issue", "issue_set", "info_units"})
        self.assertEqual(output["info_units"][0]["source_refs"][0]["source_kind"], "testimony")
        self.assertEqual(call.await_count, 2)
        self.assertEqual(call.call_args.args[2]["files"][0]["text"], "甲：我已付款")
        self.assertEqual(call.call_args.args[2]["confirmed_source_kinds"], {fid: "testimony"})
        stored = self.client.get(f"/api/cases/{case['case_id']}").json()["case"]
        self.assertEqual(stored["info_units"], output["info_units"])
        self.assertEqual(stored["relations"], [])
        run = stored["stage_runs"][-1]
        self.assertEqual(run["status"], "success")
        self.assertEqual(run["model"], "configured-model")
        self.assertTrue(run["prompt_version"].startswith("v0.3-extraction-1:"))
        root = self.data_dir / case["case_id"]
        audit = json.loads((root / run["output_snapshot_path"]).read_text(encoding="utf-8"))
        self.assertEqual(audit["model_content"], raw)
        self.assertEqual(json.loads(audit["consolidation_content"])["extraction"], json.loads(raw))
        self.assertEqual(audit["consolidation_review"]["decisions"][0]["action"], "keep")
        self.assertEqual(audit["result"], output)
        self.assertTrue((root / f"runs/{run['id']}.stage.json").is_file())

    def test_uploaded_source_kind_is_persisted_and_reused(self):
        case = self.registered_case()
        fid = case["files"][0]["file_id"]
        self.assertEqual(case["files"][0]["source_kind"], "testimony")
        reopened = case_store.load_case(case["case_id"])
        self.assertEqual(reopened.files[0].source_kind.value, "testimony")
        self.assertFalse((self.data_dir / case["case_id"] / "source_metadata.json").exists())
        self.assertEqual(self.client.put(f"/api/cases/{case['case_id']}/source-metadata",
                                        json={"source_kinds": {fid: "testimony"}}).status_code, 404)
        for _ in range(2):
            response, _ = self.run_with_reply(case, model_result([model_unit(fid, "甲：我已付款")]))
            self.assertEqual(response.status_code, 200, response.text)

    def test_invalid_missing_or_mismatched_upload_metadata_is_rejected(self):
        with patch.object(llm_client, "call_structured", new=AsyncMock()) as call:
            for data in ({"case_title": "案件"},
                         {"case_title": "案件", "source_kinds": '{}'},
                         {"case_title": "案件", "source_kinds": '{"unknown.txt":"document"}'},
                         {"case_title": "案件", "source_kinds": '{"f.docx":"invalid"}'},
                         {"case_title": "案件", "source_kinds": '{"f.docx":"record","f.docx":"testimony"}'},
                         {"case_title": "案件", "source_kinds": '["record"]'}):
                with self.subTest(data=data):
                    response = self.client.post("/api/cases", data=data, files={"files": ("f.docx", docx_bytes("材料"))})
                    self.assertEqual(response.status_code, 422, response.text)
            call.assert_not_awaited()
        self.assertEqual(list(self.data_dir.iterdir()), [])

    def test_source_kinds_bind_by_filename_not_by_order_for_create_and_append(self):
        response = self.client.post("/api/cases", data={"case_title": "案件", "source_kinds": json.dumps({
            "second.docx": "record", "first.docx": "testimony"})}, files=[
            ("files", ("first.docx", docx_bytes("证言"))), ("files", ("second.docx", docx_bytes("交易记录")))])
        self.assertEqual(response.status_code, 201, response.text)
        case = response.json()["case"]
        self.assertEqual([f["source_kind"] for f in case["files"]], ["testimony", "record"])
        response = self.client.post(f"/api/cases/{case['case_id']}/files", data={"source_kinds": json.dumps({
            "second.docx": "communication", "third.docx": "document"})}, files=[
            ("files", ("third.docx", docx_bytes("合同"))), ("files", ("second.docx", docx_bytes("沟通")))])
        self.assertEqual(response.status_code, 200, response.text)
        after = response.json()["case"]
        self.assertEqual([f["source_kind"] for f in after["files"]], ["testimony", "record", "document", "communication"])
        self.assertEqual(len({f["file_id"] for f in after["files"]}), 4)

    def test_duplicate_names_in_one_batch_and_bad_append_are_rejected(self):
        response = self.client.post("/api/cases", data={"case_title": "案件", "source_kinds": '{"same.docx":"testimony"}'}, files=[
            ("files", ("same.docx", docx_bytes("一"))), ("files", ("same.docx", docx_bytes("二")))])
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()["detail"]["code"], "duplicate_upload_filename")
        case = self.registered_case()
        before = (self.data_dir / case["case_id"] / "case.json").read_bytes()
        response = self.client.post(f"/api/cases/{case['case_id']}/files", data={"source_kinds": '{"wrong.docx":"testimony"}'},
                                    files={"files": ("new.docx", docx_bytes("新材料"))})
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual((self.data_dir / case["case_id"] / "case.json").read_bytes(), before)

    def test_all_parsed_files_are_sent_once_and_unparsed_files_are_logged(self):
        response = self.client.post("/api/cases", data={"case_title": "全案", "source_kinds": json.dumps({
            "one.txt": "testimony", "two.txt": "testimony", "scan.png": "record"})}, files=[
            ("files", ("one.txt", "甲：我已付款".encode("utf-8"))),
            ("files", ("two.txt", "乙：尚未核实".encode("utf-8"))),
            ("files", ("scan.png", b"no OCR"))])
        case = response.json()["case"]
        response, call = self.run_with_reply(case, model_result([model_unit(case["files"][0]["file_id"], "甲：我已付款")]))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(call.call_args.args[2]["files"]), 2)
        stored = case_store.load_case(case["case_id"])
        run = stored.stage_runs[-1]
        audit = json.loads((self.data_dir / case["case_id"] / run.output_snapshot_path).read_text(encoding="utf-8"))
        self.assertEqual(audit["excluded_unparsed_files"][0]["parse_status"], "unsupported")

    def test_failed_reruns_preserve_existing_valid_results_and_log_error_types(self):
        case = self.registered_case()
        fid = case["files"][0]["file_id"]
        response, _ = self.run_with_reply(case, model_result([model_unit(fid, "甲：我已付款")]))
        self.assertEqual(response.status_code, 200)
        valid = response.json()
        failures = [("{", "extraction_json_invalid"),
                    (model_result([model_unit(fid, "甲：我已付款", type="invalid")]), "extraction_schema_invalid"),
                    (model_result([model_unit(fid, "不存在")]), "extraction_source_invalid")]
        for raw, code in failures:
            with self.subTest(code=code):
                response, _ = self.run_with_reply(case, raw)
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(response.json()["detail"]["code"], code)
                stored = case_store.load_case(case["case_id"])
                self.assertEqual(stored.info_units[0].id, valid["info_units"][0]["id"])
                self.assertEqual(stored.core_issue, valid["core_issue"])
                self.assertEqual(stored.stage_runs[-1].status.value, "failed")
                run = stored.stage_runs[-1]
                audit = json.loads((self.data_dir / case["case_id"] / run.output_snapshot_path).read_text(encoding="utf-8"))
                self.assertEqual(audit["error_code"], code)
                self.assertTrue(audit["errors"])

    def test_model_call_failures_are_502_and_raw_errors_are_redacted(self):
        case = self.registered_case()
        failure = llm_client.LLMFailure("llm_call_failed", "Model provider returned HTTP 401", response="echo test-secret")
        with patch.object(llm_client, "load_config", return_value=CONFIG), \
                patch.object(llm_client, "configuration_values", return_value={"AZURE_OPENAI_API_KEY": "test-secret"}), \
                patch.object(llm_client, "call_structured", new=AsyncMock(side_effect=failure)):
            response = self.client.post("/api/extraction/run", json={"case_id": case["case_id"]})
        self.assertEqual(response.status_code, 502, response.text)
        self.assertEqual(response.json()["detail"]["code"], "llm_call_failed")
        files = list((self.data_dir / case["case_id"] / "runs").glob("*.json"))
        self.assertFalse(any("test-secret" in f.read_text(encoding="utf-8") for f in files))
        self.assertTrue(any("[REDACTED]" in f.read_text(encoding="utf-8") for f in files))

    def test_concurrent_case_change_rejects_stale_extraction(self):
        case = self.registered_case()
        fid = case["files"][0]["file_id"]
        raw = model_result([model_unit(fid, "甲：我已付款")])
        async def concurrent_change(*args):
            changed = case_store.load_case(case["case_id"])
            changed.case_title = "另一请求更新标题"
            changed.updated_at = now_iso()
            case_store.save_case(changed, set_current=False)
            return llm_client.LLMReply(raw, {"response": "valid"})
        with patch.object(llm_client, "load_config", return_value=CONFIG), \
                patch.object(llm_client, "call_structured", side_effect=concurrent_change):
            response = self.client.post("/api/extraction/run", json={"case_id": case["case_id"]})
        self.assertEqual(response.status_code, 409, response.text)
        stored = case_store.load_case(case["case_id"])
        self.assertEqual(stored.case_title, "另一请求更新标题")
        self.assertEqual(stored.info_units, [])

    def test_subset_selection_is_not_an_extraction_api_contract(self):
        case = self.registered_case()
        response = self.client.post("/api/extraction/run", json={"case_id": case["case_id"], "file_ids": []})
        self.assertEqual(response.status_code, 422, response.text)

    def test_configuration_failure_is_audited_as_503(self):
        case = self.registered_case()
        with patch.object(llm_client, 'configuration_values', return_value={}):
            response = self.client.post('/api/extraction/run', json={'case_id':case['case_id']})
        self.assertEqual(response.status_code,503,response.text)
        self.assertEqual(response.json()['detail']['code'],'llm_configuration_missing')
        self.assertEqual(case_store.load_case(case['case_id']).stage_runs[-1].status.value,'failed')

    def test_ambiguous_quote_fails_in_api_and_keeps_prior_results(self):
        case=self.registered_case('case.txt','重复\n重复\n唯一引用'.encode('utf-8'))
        fid=case['files'][0]['file_id']
        valid,_=self.run_with_reply(case,model_result([model_unit(fid,'唯一引用')]))
        self.assertEqual(valid.status_code,200,valid.text)
        response,_=self.run_with_reply(case,model_result([model_unit(fid,'重复')]))
        self.assertEqual(response.status_code,422,response.text)
        self.assertEqual(response.json()['detail']['errors'][0]['code'],'ambiguous_quote')
        self.assertEqual(case_store.load_case(case['case_id']).info_units[0].id,valid.json()['info_units'][0]['id'])

    def test_missing_cache_is_traceable_even_when_stage_index_cannot_be_saved(self):
        case=self.registered_case()
        root=self.data_dir/case['case_id']
        before=(root/'case.json').read_bytes()
        (root/case['files'][0]['parsed_text_path']).unlink()
        with patch.object(llm_client,'call_structured',new=AsyncMock()) as call:
            response=self.client.post('/api/extraction/run',json={'case_id':case['case_id']})
        self.assertEqual(response.status_code,500,response.text)
        detail=response.json()['detail']
        self.assertEqual(detail['code'],'extraction_audit_persistence_failed')
        self.assertEqual(detail['cause_code'],'parsed_cache_unavailable')
        call.assert_not_awaited()
        audit=json.loads((root/f"runs/{detail['run_id']}.output.json").read_text(encoding='utf-8'))
        self.assertEqual(audit['error_code'],'parsed_cache_unavailable')
        self.assertEqual((root/'case.json').read_bytes(),before)

    def test_audit_write_failure_never_commits_new_info_units(self):
        case=self.registered_case()
        fid=case['files'][0]['file_id']
        with patch.object(extraction_service,'_write_audit',side_effect=PermissionError('denied')):
            response,_=self.run_with_reply(case,model_result([model_unit(fid,'甲：我已付款')]))
        self.assertEqual(response.status_code,500,response.text)
        self.assertEqual(response.json()['detail']['code'],'extraction_audit_persistence_failed')
        self.assertEqual(case_store.load_case(case['case_id']).info_units,[])

    def test_explicit_consolidation_merges_paraphrases_and_keeps_multifile_sources(self):
        case=self.registered_case('first.txt','甲：已支付款项'.encode('utf-8'))
        response=self.client.post(f"/api/cases/{case['case_id']}/files",
            data={'source_kinds':'{"second.txt":"communication"}'},
            files={'files':('second.txt','甲：款项已经付出'.encode('utf-8'))})
        self.assertEqual(response.status_code,200,response.text)
        case=response.json()['case']
        a,b=case['files']
        draft=model_result([model_unit(a['file_id'],'甲：已支付款项',content='甲称已付款，未核实。'),
                            model_unit(b['file_id'],'甲：款项已经付出',content='甲表示款项已付出，未核实。')])
        combined=model_unit(a['file_id'],'甲：已支付款项',content='甲在两份材料中重复称已付款，未核实。')
        combined['source_quotes'].append({'file_id':b['file_id'],'quote':'甲：款项已经付出'})
        final=model_result([combined])
        decisions=[dict(draft_index=i,action='merge',finding='同人同义说法，合并来源。',final_indices=[0]) for i in range(2)]
        replies=[llm_client.LLMReply(draft,{'step':'extract'}),
                 llm_client.LLMReply(review_result(final, decisions),{'step':'consolidate'})]
        with patch.object(llm_client,'load_config',return_value=CONFIG), \
                patch.object(llm_client,'call_structured',new=AsyncMock(side_effect=replies)) as call:
            response=self.client.post('/api/extraction/run',json={'case_id':case['case_id']})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(call.await_count,2)
        self.assertEqual(len(response.json()['info_units']),1)
        refs=response.json()['info_units'][0]['source_refs']
        self.assertEqual({r['source_kind'] for r in refs},{'testimony','communication'})
        self.assertIn('draft_extraction',call.call_args.args[2])

    def test_consolidation_failure_is_not_a_fallback_to_the_valid_draft(self):
        case=self.registered_case()
        fid=case['files'][0]['file_id']
        valid=model_result([model_unit(fid,'甲：我已付款')])
        replies=[llm_client.LLMReply(valid,{'step':'extract'}),llm_client.LLMReply('{',{'step':'consolidate'})]
        with patch.object(llm_client,'load_config',return_value=CONFIG), \
                patch.object(llm_client,'call_structured',new=AsyncMock(side_effect=replies)):
            response=self.client.post('/api/extraction/run',json={'case_id':case['case_id']})
        self.assertEqual(response.status_code,422,response.text)
        self.assertEqual(response.json()['detail']['code'],'extraction_json_invalid')
        self.assertEqual(response.json()['detail']['step'],'consolidation')
        self.assertEqual(case_store.load_case(case['case_id']).info_units,[])

    def test_invalid_review_plan_or_final_citation_never_overwrites_valid_extraction(self):
        case = self.registered_case()
        fid = case['files'][0]['file_id']
        valid = model_result([model_unit(fid, '甲：我已付款')])
        prior, _ = self.run_with_reply(case, valid)
        self.assertEqual(prior.status_code, 200, prior.text)
        incomplete = json.loads(review_result(valid))
        incomplete['decisions'] = []
        wrong_source = review_result(model_result([model_unit(fid, '不存在的引用')]))
        for content, code in [(json.dumps(incomplete), 'extraction_schema_invalid'),
                              (wrong_source, 'extraction_source_invalid')]:
            with self.subTest(code=code):
                replies = [llm_client.LLMReply(valid, {}), llm_client.LLMReply(content, {})]
                with patch.object(llm_client, 'load_config', return_value=CONFIG), \
                        patch.object(llm_client, 'call_structured', new=AsyncMock(side_effect=replies)):
                    response = self.client.post('/api/extraction/run', json={'case_id': case['case_id']})
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(response.json()['detail']['code'], code)
                self.assertEqual(response.json()['detail']['step'], 'consolidation')
                self.assertEqual(case_store.load_case(case['case_id']).info_units[0].id,
                                 prior.json()['info_units'][0]['id'])


if __name__ == "__main__":
    unittest.main()
