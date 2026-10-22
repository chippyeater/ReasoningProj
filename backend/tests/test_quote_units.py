import copy
import hashlib
import json
import unittest

from support import IsolatedCaseTest, model_result, model_unit, quote_id_for
from app import case_store
from app.extraction import ExtractionFailure, ModelExtraction, materialize_extraction, parse_model_extraction
from app.quote_units import (
    MAX_QUOTE_CHARS, MAX_RECORD_CHARS, SEGMENTATION_VERSION, QuoteIndexEvent,
    build_quote_model_input, load_or_build_quote_index, quote_index_relative_path,
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
        self.assertEqual(set(value["files"][0]["quote_units"][0]), {
            "quote_id", "text", "role", "group_id", "message_id", "context_quote_ids"})
        schema = json.dumps(ModelExtraction.model_json_schema())
        self.assertIn("evidence_ids", schema)
        self.assertNotIn("source_quotes", schema)

    def test_chat_date_time_groups_are_linked_by_quote_id(self):
        text = ("2026-03-01\n张某：我已经找到朋友接租\n李某：可以再谈\n10:31 王某：收到\n---\n"
                "2026年3月5日\n[10:30]\n张某：我这周搬走了")
        saved = self.upload(raw=text.encode("utf-8"), source_kind="communication")
        case = case_store.load_case(saved["case_id"])
        index = load_or_build_quote_index(case.case_id, case.files[0])
        by_text = {unit.text: unit for unit in index.units}
        self.assertEqual(by_text["---"].role, "group_boundary")
        with self.assertRaises(ExtractionFailure) as boundary_error:
            materialize_extraction(parse_model_extraction(model_result([
                model_unit(case.files[0].file_id, by_text["---"].quote_id)
            ])), case)
        self.assertEqual(boundary_error.exception.errors[0]["code"], "quote_unit_not_evidence")
        first_date = by_text["2026-03-01"]
        second_date = by_text["2026年3月5日"]
        time = by_text["[10:30]"]
        first_messages = [by_text["张某：我已经找到朋友接租"], by_text["李某：可以再谈"],
                          by_text["10:31 王某：收到"]]
        for message in first_messages:
            self.assertEqual(message.context_quote_ids, [first_date.quote_id])
            self.assertEqual(message.group_id, first_date.group_id)
        last = by_text["张某：我这周搬走了"]
        self.assertEqual(last.context_quote_ids, [second_date.quote_id, time.quote_id])
        self.assertEqual(time.context_quote_ids, [second_date.quote_id])
        self.assertEqual(last.group_id, second_date.group_id)
        self.assertNotEqual(first_date.group_id, second_date.group_id)

    def test_unrecognized_chat_format_does_not_invent_context(self):
        text = "2026-99-99\n没有可确认说话人的内容\n张某：可确认的消息"
        saved = self.upload(raw=text.encode("utf-8"), source_kind="communication")
        case = case_store.load_case(saved["case_id"])
        units = load_or_build_quote_index(case.case_id, case.files[0]).units
        self.assertEqual([unit.role for unit in units[:2]], ["text", "text"])
        self.assertTrue(all(unit.group_id is None and not unit.context_quote_ids for unit in units[:2]))
        message = units[2]
        self.assertEqual(message.role, "message")
        self.assertEqual(message.context_quote_ids, [])

    def test_multiline_long_message_keeps_message_and_date_identity(self):
        body = "张某：首行\n" + "后续内容" * 500
        text = "2026-03-01\n" + body
        saved = self.upload(raw=text.encode("utf-8"), source_kind="communication")
        case = case_store.load_case(saved["case_id"])
        units = load_or_build_quote_index(case.case_id, case.files[0]).units
        date_unit = units[0]
        pieces = units[1:]
        self.assertGreater(len(pieces), 1)
        self.assertTrue(all(piece.role == "message" for piece in pieces))
        self.assertEqual(len({piece.message_id for piece in pieces}), 1)
        self.assertEqual(len({piece.group_id for piece in pieces}), 1)
        self.assertTrue(all(piece.context_quote_ids == [date_unit.quote_id] for piece in pieces))
        self.assertTrue(all(len(piece.text) <= MAX_QUOTE_CHARS for piece in pieces))

    def test_date_context_does_not_pollute_source_ref_or_replace_evidence(self):
        text = "2026-03-01\n张某：我已经搬走了"
        saved = self.upload(raw=text.encode("utf-8"), source_kind="communication")
        case = case_store.load_case(saved["case_id"])
        fid = case.files[0].file_id
        index = load_or_build_quote_index(case.case_id, case.files[0])
        message = next(unit for unit in index.units if unit.role == "message")
        diagnostics = {}
        result, _ = materialize_extraction(parse_model_extraction(model_result([
            model_unit(fid, message.quote_id, content="张某称于2026年3月1日搬走，尚未经核实。")
        ])), case, diagnostics)
        source = result.info_units[0].source_refs[0]
        self.assertEqual(source.quote, "张某：我已经搬走了")
        self.assertNotIn("2026-03-01", source.quote)
        findings = diagnostics["candidates"][0]["support_review"]["findings"]
        self.assertIn("date_not_in_selected_evidence", {item["code"] for item in findings})
        date_unit = next(unit for unit in index.units if unit.role == "temporal_context")
        candidate = model_unit(fid, date_unit.quote_id,
                               content="张某称于2026年3月1日搬走，尚未经核实。")
        candidate["evidence_ids"].append(message.quote_id)
        traced, _ = materialize_extraction(parse_model_extraction(model_result([candidate])), case)
        self.assertEqual([ref.quote for ref in traced.info_units[0].source_refs],
                         ["2026-03-01", "张某：我已经搬走了"])

    def test_unpunctuated_long_text_and_pdf_hard_wraps_keep_all_content(self):
        text = ("中文English编号A-2026-001金额2000元" * 100
                + "\nPDF提取造成的异常换行 mixed text 继续" * 20)
        saved = self.upload(raw=text.encode("utf-8"), source_kind="document")
        case = case_store.load_case(saved["case_id"])
        units = load_or_build_quote_index(case.case_id, case.files[0]).units
        self.assertGreater(len(units), 1)
        self.assertTrue(all(len(unit.text) <= MAX_QUOTE_CHARS for unit in units))
        covered = set()
        for unit in units:
            covered.update(range(unit.start, unit.end))
        self.assertTrue(all(char.isspace() or index in covered for index, char in enumerate(text)))

    def test_long_record_pieces_keep_one_record_group(self):
        text = "时间：2026-01-05\n付款人：张某\n备注：" + "无标点内容" * 700
        saved = self.upload(raw=text.encode("utf-8"), source_kind="record")
        case = case_store.load_case(saved["case_id"])
        units = load_or_build_quote_index(case.case_id, case.files[0]).units
        self.assertGreater(len(units), 1)
        self.assertTrue(all(unit.role == "record" and len(unit.text) <= MAX_RECORD_CHARS for unit in units))
        self.assertEqual(len({unit.group_id for unit in units}), 1)

    def test_deleted_middle_unit_triggers_complete_rebuild(self):
        saved = self.upload(raw="第一句。第二句。第三句。".encode("utf-8"), source_kind="document")
        case = case_store.load_case(saved["case_id"])
        file = case.files[0]
        original = load_or_build_quote_index(case.case_id, file)
        path = self.data_dir / case.case_id / quote_index_relative_path(file.file_id)
        payload = original.model_dump(mode="json")
        del payload["units"][1]
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        events: list[QuoteIndexEvent] = []
        rebuilt = load_or_build_quote_index(case.case_id, file, events)
        self.assertEqual((events[0].action, events[0].reason), ("rebuilt", "invalid"))
        self.assertEqual(rebuilt.model_dump(mode="json"), original.model_dump(mode="json"))

    def test_offset_id_context_group_and_order_tampering_each_rebuilds(self):
        text = "2026-03-01\n张某：第一条\n李某：第二条"
        saved = self.upload(raw=text.encode("utf-8"), source_kind="communication")
        case = case_store.load_case(saved["case_id"])
        file = case.files[0]
        original = load_or_build_quote_index(case.case_id, file).model_dump(mode="json")
        path = self.data_dir / case.case_id / quote_index_relative_path(file.file_id)

        def offset(payload):
            payload["units"][1]["start"] += 1
        def quote_id(payload):
            payload["units"][1]["quote_id"] = "quote-forged"
        def context(payload):
            payload["units"][1]["context_quote_ids"] = ["quote-missing"]
        def group(payload):
            payload["units"][1]["group_id"] = "group-forged"
        def order(payload):
            payload["units"][1], payload["units"][2] = payload["units"][2], payload["units"][1]

        for name, mutate in [("offset", offset), ("id", quote_id), ("context", context),
                             ("group", group), ("order", order)]:
            with self.subTest(name=name):
                payload = copy.deepcopy(original)
                mutate(payload)
                path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                events: list[QuoteIndexEvent] = []
                rebuilt = load_or_build_quote_index(case.case_id, file, events)
                self.assertEqual((events[0].action, events[0].reason), ("rebuilt", "invalid"))
                self.assertEqual(rebuilt.model_dump(mode="json"), original)

    def test_old_segmentation_version_rebuilds_and_invalidates_old_id(self):
        saved = self.upload(raw="版本迁移原文。".encode("utf-8"), source_kind="document")
        case = case_store.load_case(saved["case_id"])
        file = case.files[0]
        current = load_or_build_quote_index(case.case_id, file)
        path = self.data_dir / case.case_id / quote_index_relative_path(file.file_id)
        payload = current.model_dump(mode="json")
        unit = payload["units"][0]
        old_material = (f"quote-units-v1\0{file.file_id}\0{current.text_sha256}\0"
                        f"{unit['start']}\0{unit['end']}\0{unit['text']}").encode("utf-8")
        old_id = "quote-" + hashlib.sha256(old_material).hexdigest()[:24]
        payload["segmentation_version"] = "quote-units-v1"
        payload["units"][0]["quote_id"] = old_id
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        events: list[QuoteIndexEvent] = []
        rebuilt = load_or_build_quote_index(case.case_id, file, events)
        self.assertEqual(events[0].reason, "segmentation_version_changed")
        self.assertEqual(rebuilt.segmentation_version, SEGMENTATION_VERSION)
        self.assertNotIn(old_id, {item.quote_id for item in rebuilt.units})

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
