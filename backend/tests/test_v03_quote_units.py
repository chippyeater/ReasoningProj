import json
import unittest

from support import IsolatedCaseTest, quote_id_for
from test_v02_extraction import model_result, model_unit
from app import case_store
from app.extraction import ExtractionFailure, ModelExtraction, materialize_extraction, parse_model_extraction
from app.quote_units import (
    QuoteIndexEvent, build_quote_model_input, load_or_build_quote_index,
    quote_index_relative_path,
)


class TestQuoteUnits(IsolatedCaseTest):
    def test_repeated_build_is_stable_and_reuses_persisted_index(self):
        saved = self.upload(raw="第一条。\n第二条。".encode("utf-8"), source_kind="document")
        case = case_store.load_case(saved["case_id"])
        file = case.files[0]
        first_events: list[QuoteIndexEvent] = []
        first = load_or_build_quote_index(case.case_id, file, first_events)
        target = self.data_dir / case.case_id / quote_index_relative_path(file.file_id)
        raw = target.read_bytes()
        second_events: list[QuoteIndexEvent] = []
        second = load_or_build_quote_index(case.case_id, file, second_events)
        self.assertEqual([unit.quote_id for unit in first.units], [unit.quote_id for unit in second.units])
        self.assertEqual(target.read_bytes(), raw)
        self.assertEqual((first_events[0].action, first_events[0].reason), ("rebuilt", "missing"))
        self.assertEqual((second_events[0].action, second_events[0].reason), ("reused", "current"))

    def test_every_unit_is_an_exact_unicode_slice(self):
        saved = self.upload(raw="😀第一句。第二句！\n末行".encode("utf-8"), source_kind="testimony")
        case = case_store.load_case(saved["case_id"])
        file = case.files[0]
        text = case_store.read_parsed_document(case.case_id, file).text
        index = load_or_build_quote_index(case.case_id, file)
        self.assertGreaterEqual(len(index.units), 3)
        for unit in index.units:
            self.assertEqual(text[unit.start:unit.end], unit.text)
            self.assertEqual(unit.end - unit.start, len(unit.text))

    def test_identical_text_in_different_files_has_distinct_ids(self):
        response = self.client.post("/api/cases", data={"case_title": "same", "source_kinds": json.dumps({
            "a.txt": "testimony", "b.txt": "testimony"})}, files=[
            ("files", ("a.txt", "相同句子。".encode("utf-8"))),
            ("files", ("b.txt", "相同句子。".encode("utf-8"))),
        ])
        self.assertEqual(response.status_code, 201, response.text)
        case = response.json()["case"]
        ids = [quote_id_for(case["case_id"], file["file_id"], "相同句子。") for file in case["files"]]
        self.assertEqual(len(set(ids)), 2)

    def test_material_types_use_structural_segmentation(self):
        payloads = {
            "contract.txt": ("document", "第一条 租期六个月。第二条 月租三千元。"),
            "chat.txt": ("communication", "2026-03-01\n张某：我找到朋友接租\n李某：可以再谈"),
            "statement.txt": ("testimony", "我已经搬走了。房屋没有损坏。"),
            "transfer.txt": ("record", "时间：2026-01-05\n付款人：张某\n金额：2000元"),
        }
        response = self.client.post("/api/cases", data={"case_title": "types", "source_kinds": json.dumps({
            name: kind for name, (kind, _) in payloads.items()}, ensure_ascii=False)}, files=[
            ("files", (name, text.encode("utf-8"))) for name, (_, text) in payloads.items()
        ])
        self.assertEqual(response.status_code, 201, response.text)
        case = case_store.load_case(response.json()["case"]["case_id"])
        counts = {file.filename: len(load_or_build_quote_index(case.case_id, file).units) for file in case.files}
        self.assertEqual(counts["contract.txt"], 2)
        self.assertEqual(counts["chat.txt"], 3)
        self.assertEqual(counts["statement.txt"], 2)
        self.assertEqual(counts["transfer.txt"], 1)

    def test_model_input_contains_quote_units_without_duplicate_full_text(self):
        saved = self.upload(raw="第一句。\n第二句。".encode("utf-8"), source_kind="document")
        case = case_store.load_case(saved["case_id"])
        value = build_quote_model_input(case)
        self.assertNotIn("text", value["files"][0])
        self.assertEqual(set(value["files"][0]["quote_units"][0]), {"quote_id", "text"})
        schema = json.dumps(ModelExtraction.model_json_schema())
        self.assertIn("evidence_ids", schema)
        self.assertNotIn("source_quotes", schema)

    def test_multiple_and_noncontiguous_evidence_become_separate_source_refs(self):
        saved = self.upload(raw="第一项事实。\n无关内容。\n第二项事实。".encode("utf-8"), source_kind="document")
        case = case_store.load_case(saved["case_id"])
        fid = case.files[0].file_id
        first = quote_id_for(case.case_id, fid, "第一项事实。")
        second = quote_id_for(case.case_id, fid, "第二项事实。")
        unit = model_unit(fid, first, content="材料分别记载第一项和第二项事实。")
        unit["evidence_ids"].append(second)
        result, _ = materialize_extraction(parse_model_extraction(model_result([unit])), case)
        refs = result.info_units[0].source_refs
        self.assertEqual([ref.quote for ref in refs], ["第一项事实。", "第二项事实。"])
        self.assertLess(refs[0].end, refs[1].start)

    def test_invalid_corrupt_and_stale_ids_are_not_guessed(self):
        saved = self.upload(raw="原始事实。".encode("utf-8"), source_kind="testimony")
        case = case_store.load_case(saved["case_id"])
        file = case.files[0]
        valid_id = quote_id_for(case.case_id, file.file_id, "原始事实。")
        index_path = self.data_dir / case.case_id / quote_index_relative_path(file.file_id)
        payload = json.loads(index_path.read_text(encoding="utf-8"))
        payload["units"][0]["quote_id"] = "quote-forged"
        payload["units"][0]["end"] += 100
        index_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        events: list[QuoteIndexEvent] = []
        rebuilt = load_or_build_quote_index(case.case_id, file, events)
        self.assertEqual((events[0].action, events[0].reason), ("rebuilt", "invalid"))
        self.assertEqual(rebuilt.units[0].quote_id, valid_id)
        with self.assertRaises(ExtractionFailure) as invalid:
            materialize_extraction(parse_model_extraction(model_result([
                model_unit(file.file_id, "quote-forged")])), case)
        self.assertEqual(invalid.exception.errors[0]["code"], "invalid_evidence_id")

        parsed = self.data_dir / case.case_id / file.parsed_text_path
        parsed.write_text("已改变的事实。", encoding="utf-8")
        with self.assertRaises(ExtractionFailure) as stale:
            materialize_extraction(parse_model_extraction(model_result([
                model_unit(file.file_id, valid_id)])), case)
        self.assertEqual(stale.exception.errors[0]["code"], "stale_evidence_id")

    def test_amount_and_date_mismatch_is_explained_without_false_rejection(self):
        saved = self.upload(raw="2026年3月5日支付2000元。".encode("utf-8"), source_kind="record")
        case = case_store.load_case(saved["case_id"])
        fid = case.files[0].file_id
        evidence_id = quote_id_for(case.case_id, fid, "2026年3月5日支付2000元。")
        diagnostics = {}
        result, _ = materialize_extraction(parse_model_extraction(model_result([
            model_unit(fid, evidence_id, content="2026年3月6日支付3000元，尚未经独立核实。")
        ])), case, diagnostics)
        self.assertEqual(len(result.info_units), 1)
        review = diagnostics["candidates"][0]["support_review"]
        self.assertEqual(review["deterministic_status"], "needs_review")
        self.assertEqual({item["code"] for item in review["findings"]},
                         {"amount_not_in_selected_evidence", "date_not_in_selected_evidence"})
        self.assertEqual(review["semantic_support"], "needs_review")


if __name__ == "__main__":
    unittest.main()
