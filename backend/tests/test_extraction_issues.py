"""Issue reuse over real upload/Extraction/storage boundaries; model replies are offline."""
import json

from support import IsolatedCaseTest, model_unit, quote_id_for
import test_extraction_api as api_fixture
from app import case_store
from app.issue_workspaces import get_issue_workspaces


class TestExtractionIssues(IsolatedCaseTest):
    run_with_reply = api_fixture.TestExtractionAPI.run_with_reply
    issue = "合同付款责任"

    def extract(self, uploaded, text, *, labels=None, core=None, related=None, content=None):
        labels = [self.issue] if labels is None else labels
        fid = uploaded["files"][-1]["file_id"]
        unit = model_unit(fid, quote_id_for(uploaded["case_id"], fid, text),
                          related_issue=related if related is not None else labels[-1],
                          content=content or "甲主张乙未按约付款，尚待核实。")
        response, call = self.run_with_reply(uploaded, json.dumps({
            "core_issue": core, "issue_set": labels, "info_units": [unit]}, ensure_ascii=False))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(call.await_count, 1)
        return case_store.load_case(uploaded["case_id"]), call

    def initial(self, *, null_core=False):
        text = "甲主张乙未按约付款。"
        uploaded = self.upload(raw=text.encode("utf-8"), source_kind="testimony")
        case, _ = self.extract(uploaded, text, core=None if null_core else self.issue)
        return case

    def append(self, case, text):
        response = self.client.post(f"/api/cases/{case.case_id}/files", data={"source_kinds": '{"new.txt":"testimony"}'},
                                    files={"files": ("new.txt", text.encode("utf-8"))})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["case"]

    def workspaces(self, case):
        uid = case.info_units[0].id
        def edit(**values):
            result = self.client.patch(f"/api/cases/{case.case_id}/issue-reasoning", json={"issue": self.issue, **values})
            self.assertEqual(result.status_code, 200, result.text)
            return result.json()["object_id"]
        hid = edit(hypothesis={"content": "乙可能未履行付款义务"})
        edit(gap={"content": "实际到账及付款归属仍待核实", "hypothesis_ids": [hid], "basis_info_unit_ids": [uid]})
        edit(link={"hypothesis_id": hid, "info_unit_id": uid, "meaning": "related", "rationale": "付款主张有待核实"})
        current = get_issue_workspaces(case)
        canvas = current.workspaces[self.issue].canvas.model_dump(mode="json")
        canvas.update(selected_node_ids=[hid], focused_node_id=hid, viewport={"zoom": 0.8, "offset_x": 80, "offset_y": 20})
        canvas["node_positions"][hid] = {"x": 741, "y": 318}
        response = self.client.patch(f"/api/cases/{case.case_id}/issue-workspaces", json={"issue": self.issue, "canvas": canvas})
        self.assertEqual(response.status_code, 200, response.text)
        path = self.data_dir / case.case_id / "issue_workspaces.json"
        return path, path.read_bytes(), get_issue_workspaces(case).workspaces[self.issue]

    def test_first_input_has_no_prior_issues_and_selects_dispute_not_record(self):
        text = "乙提交转账记录，双方仍对付款责任存在争议。"
        uploaded = self.upload(raw=text.encode("utf-8"), source_kind="testimony")
        case, call = self.extract(uploaded, text, core=self.issue, content="乙提交转账记录，到账情况仍待核实。")
        model_input = call.call_args.args[2]
        self.assertEqual(model_input["existing_issue_set"], [])
        self.assertIsNone(model_input["existing_core_issue"])
        self.assertEqual(case.issue_set, [self.issue])
        self.assertEqual(case.info_units[0].related_issue, self.issue)
        # Mock verifies the boundary, not the semantic quality of a real model.
        prompt = call.call_args.args[1]
        self.assertIn("案件争议焦点", prompt)
        self.assertIn("Evidence Gap", prompt)

    def test_incremental_model_reuses_original_label_for_new_evidence_and_preserves_workspace(self):
        old = self.initial()
        path, raw, original = self.workspaces(old)
        for index in range(2):
            text = f"乙第{index + 1}次补充同一付款争议的转账凭证。"
            uploaded = self.append(old, text)
            current, call = self.extract(uploaded, text, core=self.issue,
                                         content=f"乙补充第{index + 1}份付款凭证，实际到账仍待核实。")
            model_input = call.call_args.args[2]
            self.assertEqual(model_input["existing_issue_set"], [self.issue])
            self.assertEqual(model_input["existing_core_issue"], self.issue)
            self.assertEqual([f["file_id"] for f in model_input["files"]], [uploaded["files"][-1]["file_id"]])
            self.assertNotIn("info_units", model_input)
            self.assertEqual(current.issue_set, [self.issue])
            self.assertEqual(current.core_issue, self.issue)
            self.assertEqual({u.related_issue for u in current.info_units}, {self.issue})
            self.assertEqual(current.info_units[0], old.info_units[0])
            self.assertEqual(path.read_bytes(), raw)
            self.assertEqual(get_issue_workspaces(current).workspaces[self.issue], original)
            old = current

    def test_incoming_format_variant_reuses_saved_key_before_info_unit_deduplication(self):
        old = self.initial()
        path, raw, original = self.workspaces(old)
        text = "甲再次提交同一付款主张。"
        alias = "  合同付款责任？ "
        current, _ = self.extract(self.append(old, text), text, labels=[alias], core=alias)
        self.assertEqual(current.core_issue, old.core_issue)
        self.assertEqual(current.issue_set, [self.issue])
        self.assertEqual(len(current.info_units), 1)
        self.assertEqual(current.info_units[0].id, old.info_units[0].id)
        self.assertEqual(current.info_units[0].related_issue, self.issue)
        self.assertEqual(len(current.info_units[0].source_refs), 2)
        self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(get_issue_workspaces(current).workspaces[self.issue], original)
        run = current.stage_runs[-1]
        audit = json.loads((self.data_dir / current.case_id / run.output_snapshot_path).read_text(encoding="utf-8"))
        self.assertEqual(audit["parsed_result"]["issue_set"], [alias])
        self.assertEqual(audit["materialization"]["issue_aliases"], [{"proposed_issue": alias, "resolved_issue": self.issue}])

    def test_independent_new_dispute_appends_without_renaming_old_issues_or_core(self):
        old = self.initial()
        path, raw, original = self.workspaces(old)
        new_issue = "合同效力"
        text = "双方新增对合同效力的争议。"
        current, _ = self.extract(self.append(old, text), text, labels=[new_issue], core=new_issue,
                                 content="双方对合同效力另有争议，尚待判断。")
        self.assertEqual(current.issue_set, [self.issue, new_issue])
        self.assertEqual(current.core_issue, self.issue)
        self.assertEqual(current.info_units[0], old.info_units[0])
        self.assertEqual(current.info_units[-1].related_issue, new_issue)
        self.assertEqual(path.read_bytes(), raw)
        projected = get_issue_workspaces(current)
        self.assertEqual(projected.workspaces[self.issue], original)
        self.assertEqual(projected.workspaces[new_issue].canvas.visible_node_ids, [current.info_units[-1].id])

    def test_null_core_stays_null_when_existing_issue_is_reused_or_new_dispute_added(self):
        old = self.initial(null_core=True)
        for label, text in [(self.issue, "甲补充付款凭证。"), ("合同效力", "双方另有合同效力争议。")]:
            current, call = self.extract(self.append(old, text), text, labels=[label], core=label,
                                         content=f"材料涉及{label}，仍待核实。")
            self.assertIsNone(call.call_args.args[2]["existing_core_issue"])
            self.assertIsNone(current.core_issue)
            self.assertEqual(current.issue_set, list(dict.fromkeys([*old.issue_set, label])))
            self.assertIn(current.info_units[-1].related_issue, current.issue_set)
            old = current

    def test_multiple_saved_format_equivalents_are_preserved_and_ambiguous_new_alias_fails(self):
        old = self.initial()
        old.issue_set.append(self.issue + "？")
        case_store.save_case(old, set_current=False)
        path, raw, original = self.workspaces(old)
        text = "甲补充付款材料。"
        uploaded = self.append(old, text)
        fid = uploaded["files"][-1]["file_id"]
        label = " 合同付款责任。 "
        response, call = self.run_with_reply(uploaded, json.dumps({
            "core_issue": None, "issue_set": [label], "info_units": [model_unit(fid,
                quote_id_for(old.case_id, fid, text), related_issue=label)]}, ensure_ascii=False))
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(call.await_count, 1)
        self.assertEqual(response.json()["detail"]["code"], "extraction_issue_ambiguous")
        current = case_store.load_case(old.case_id)
        self.assertEqual(current.issue_set, old.issue_set)
        self.assertEqual(current.info_units, old.info_units)
        self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(get_issue_workspaces(current).workspaces[self.issue], original)
        # An exact saved key remains selectable even when old labels look equivalent.
        current, _ = self.extract(uploaded, text, labels=[self.issue + "？"], core=None,
                                 content="甲补充付款材料，内容有待核实。")
        self.assertEqual(current.issue_set, old.issue_set)
        self.assertEqual(current.info_units[-1].related_issue, self.issue + "？")

    def test_first_batch_format_duplicates_resolve_core_and_all_related_issues_together(self):
        text = "双方争议合同付款责任。"
        uploaded = self.upload(raw=text.encode("utf-8"), source_kind="testimony")
        alias = self.issue + "？"
        current, _ = self.extract(uploaded, text, labels=[self.issue, alias], core=alias)
        self.assertEqual(current.issue_set, [self.issue])
        self.assertEqual(current.core_issue, self.issue)
        self.assertEqual(current.info_units[0].related_issue, self.issue)

    def test_different_conditions_numbers_and_internal_punctuation_are_not_fuzzily_merged(self):
        from app.extraction import _resolve_issue_labels
        for first, second in [("付款责任（已交付）", "付款责任（未交付）"), ("第1期付款责任", "第2期付款责任"),
                              ("利率1.5%的效力", "利率15%的效力"), ("A B 的付款责任", "AB 的付款责任")]:
            with self.subTest(first=first, second=second):
                labels, mapping = _resolve_issue_labels([first], [second])
                self.assertEqual(labels, [first, second])
                self.assertEqual(mapping[second], second)
