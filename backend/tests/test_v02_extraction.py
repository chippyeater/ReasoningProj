import json
from itertools import count
import unittest

from support import IsolatedCaseTest, quote_id_for
from app import case_store
from app.extraction import (
    ExtractionFailure, deduplicate_units, materialize_extraction,
    parse_model_extraction,
)
from app.schemas import SourceKind


_LOCAL_IDS = count()


def model_unit(_file_id, evidence_id, **changes):
    value = dict(local_id=f"local-{next(_LOCAL_IDS)}", type="event", subtype="reported", title="付款陈述",
                 content="甲称已付款，尚未经独立核实。", evidence_ids=[evidence_id],
                 speaker="甲", side="付款方", actor_local_id=None, target_local_id=None,
                 related_issue="款项是否已支付")
    value.update(changes)
    return value


def model_result(units):
    return json.dumps(dict(core_issue="款项是否已支付", issue_set=["款项是否已支付"], info_units=units), ensure_ascii=False)


class TestExtractionBoundary(IsolatedCaseTest):
    def test_materialization_generates_ids_time_and_unicode_positions(self):
        saved = self.upload(raw="😀\n甲：我已付款".encode("utf-8"), source_kind="testimony")
        case = case_store.load_case(saved["case_id"])
        file_id = case.files[0].file_id
        evidence_id = quote_id_for(case.case_id, file_id, "甲：我已付款")
        result, merges = materialize_extraction(
            parse_model_extraction(model_result([model_unit(file_id, evidence_id)])), case)
        unit = result.info_units[0]
        self.assertTrue(unit.id.startswith("info-"))
        self.assertTrue(unit.created_at.endswith("Z"))
        ref = unit.source_refs[0]
        self.assertEqual((ref.start, ref.end), (2, 8))
        self.assertEqual(ref.source_kind, SourceKind.testimony)
        self.assertEqual(merges, [])

    def test_nonjson_fences_duplicate_keys_and_constants_fail_as_json_errors(self):
        for raw in ("```json\n{}\n```", "{", '{"x":1,"x":2}', '{"core_issue":NaN}'):
            with self.subTest(raw=raw), self.assertRaises(ExtractionFailure) as caught:
                parse_model_extraction(raw)
            self.assertEqual(caught.exception.code, "extraction_json_invalid")

    def test_invalid_fields_types_and_classification_are_schema_errors(self):
        base = model_unit("f", "原文")
        changes = [dict(type="unknown"), dict(type="entity", subtype="reported"), dict(content=123),
                   dict(content="  "), dict(evidence_ids=[]), dict(id="model-id"), dict(created_at="model-time"),
                   dict(source_quotes=[dict(file_id="f", quote="原文")]),
                   dict(evidence_ids=[{"quote_id": "not-a-string"}])]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ExtractionFailure) as caught:
                parse_model_extraction(model_result([{**base, **change}]))
            self.assertEqual(caught.exception.code, "extraction_schema_invalid")
        value = dict(base)
        del value["side"]
        with self.assertRaises(ExtractionFailure):
            parse_model_extraction(model_result([value]))

    def test_stance_and_core_issue_are_validated_before_materialization(self):
        value = json.loads(model_result([model_unit("f", "原文")]))
        for change in (dict(core_issue="未定义"), dict(issue_set=["重复", "重复"])):
            with self.subTest(change=change), self.assertRaises(ExtractionFailure):
                parse_model_extraction(json.dumps({**value, **change}, ensure_ascii=False))
        with self.assertRaises(ExtractionFailure):
            parse_model_extraction(model_result([model_unit("f", "原文", related_issue="未定义")]))

    def test_cross_file_duplicates_merge_and_preserve_all_sources(self):
        saved = self.upload(raw="甲：我已付款".encode("utf-8"), source_kind="testimony")
        response = self.client.post(f"/api/cases/{saved['case_id']}/files",
                                    data={"source_kinds": '{"second.txt":"communication"}'},
                                    files={"files": ("second.txt", "甲再次说：我已付款".encode("utf-8"))})
        self.assertEqual(response.status_code, 200, response.text)
        case = case_store.load_case(saved["case_id"])
        a, b = case.files
        qa = quote_id_for(case.case_id, a.file_id, "甲：我已付款")
        qb = quote_id_for(case.case_id, b.file_id, "甲再次说：我已付款")
        draft = parse_model_extraction(model_result([
            model_unit(a.file_id, qa), model_unit(b.file_id, qb)]))
        result, merges = materialize_extraction(draft, case)
        self.assertEqual(len(result.info_units), 1)
        self.assertEqual(len(result.info_units[0].source_refs), 2)
        self.assertEqual(len(merges), 1)
        self.assertEqual({r.source_kind for r in result.info_units[0].source_refs},
                         {SourceKind.testimony, SourceKind.communication})
        self.assertEqual(result.info_units[0].speaker, "甲")
        self.assertIn("尚未经独立核实", result.info_units[0].content)

    def test_conflicting_statements_different_speakers_and_times_are_not_merged(self):
        saved = self.upload(raw="甲称付款\n乙称未收款\n甲称次日付款".encode("utf-8"))
        case = case_store.load_case(saved["case_id"])
        fid = case.files[0].file_id
        units = [model_unit(fid, quote_id_for(case.case_id, fid, "甲称付款")),
                 model_unit(fid, quote_id_for(case.case_id, fid, "乙称未收款"),
                            content="乙称未收款。", speaker="乙", side="收款方"),
                 model_unit(fid, quote_id_for(case.case_id, fid, "甲称次日付款"), content="甲称次日付款。")]
        result, _ = materialize_extraction(parse_model_extraction(model_result(units)), case)
        self.assertEqual(len(result.info_units), 3)
        copied = result.info_units[0].model_copy(deep=True)
        copied.speaker = "另一人"
        copied.id = "different-person"
        self.assertEqual(len(deduplicate_units([result.info_units[0], copied])[0]), 2)

    def test_duplicate_evidence_ids_are_rejected(self):
        saved = self.upload(raw="甲称付款".encode("utf-8"))
        case = case_store.load_case(saved["case_id"])
        fid = case.files[0].file_id
        unit = model_unit(fid, quote_id_for(case.case_id, fid, "甲称付款"))
        unit["evidence_ids"] *= 2
        with self.assertRaises(ExtractionFailure) as caught:
            parse_model_extraction(model_result([unit]))
        self.assertEqual(caught.exception.code, "extraction_schema_invalid")

    def test_invalid_quote_unit_ids_report_errors(self):
        saved = self.upload(raw="重复\n重复\n真实引文".encode("utf-8"))
        case = case_store.load_case(saved["case_id"])
        fid = case.files[0].file_id
        with self.assertRaises(ExtractionFailure) as caught:
            materialize_extraction(parse_model_extraction(
                model_result([model_unit(fid, "quote-does-not-exist")])), case)
        self.assertEqual(caught.exception.code, "extraction_source_invalid")
        self.assertEqual(caught.exception.errors[0]["code"], "invalid_evidence_id")
        self.assertEqual(caught.exception.errors[0]["candidate_id"], "quote-does-not-exist")

    def test_same_model_output_gets_new_formal_ids_each_run(self):
        saved = self.upload(raw="甲称付款".encode("utf-8"))
        case = case_store.load_case(saved["case_id"])
        fid = case.files[0].file_id
        draft = parse_model_extraction(model_result([
            model_unit(fid, quote_id_for(case.case_id, fid, "甲称付款"))]))
        first, _ = materialize_extraction(draft, case)
        second, _ = materialize_extraction(draft, case)
        self.assertNotEqual(first.info_units[0].id, second.info_units[0].id)
        self.assertEqual(case.info_units, [])


if __name__ == "__main__":
    unittest.main()
