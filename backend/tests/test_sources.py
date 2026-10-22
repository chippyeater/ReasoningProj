import json
import unittest

from support import GOLD, IsolatedCaseTest, gold_lines
from app import case_store
from app.case_service import build_extraction_input
from app.errors import AppError
from app.schemas import (
    CaseFile, ExtractionOutput, RelationInput, RelationOutput, SourceKind, ValidationInput,
)
from app.validation import locate_source_ref, validate_extraction, validate_relations
class TestSources(IsolatedCaseTest):
    def gold_validation_input(self):
        manifest = json.loads((GOLD / "fixtures/file_manifest.json").read_text(encoding="utf-8"))
        files = []
        case_id = "case-gold-validation"
        for record in manifest["files"]:
            raw = (GOLD / "fixtures/raw" / record["filename"]).read_bytes()
            parsed = (GOLD / record["fixture_text_path"]).read_bytes()
            case_store.atomic_write(case_store.artifact_path(case_id, record["original_path"]), raw)
            case_store.atomic_write(case_store.artifact_path(case_id, record["parsed_text_path"]), parsed)
            files.append(CaseFile(
                file_id=record["file_id"], filename=record["filename"], file_type=record["file_type"],
                file_size=len(raw), original_path=record["original_path"], source_kind=record["source_kind"],
                parsed_text_path=record["parsed_text_path"], parse_status="parsed", error_message=None,
                uploaded_at="2026-10-20T00:00:00Z"))
        issues = json.loads((GOLD / "gold/gold_issues.json").read_text(encoding="utf-8"))
        extraction = ExtractionOutput(core_issue=issues["core_issue"], issue_set=issues["issue_set"],
                                      info_units=gold_lines("gold_info_units.jsonl"))
        return case_id, ValidationInput(extraction=extraction, parsed_files=files)

    def test_all_gold_source_refs_match_cached_unicode_slices_and_metadata(self):
        case_id, value = self.gold_validation_input()
        result = validate_extraction(case_id, value)
        self.assertTrue(result.valid, result.errors)
        self.assertEqual(result.data, value.extraction)
        self.assertEqual(result.errors, [])

    def test_locator_computes_unicode_positions_from_cached_txt(self):
        saved = self.upload(raw="前缀😀\r\n证据原文\n结束".encode("utf-8"))
        case = case_store.load_case(saved["case_id"])
        ref = locate_source_ref(case.case_id, case.files[0], "证据原文")
        self.assertEqual((ref.start, ref.end), (4, 8))
        text = build_extraction_input(case).files[0].text
        self.assertEqual(text[ref.start:ref.end], ref.quote)

    def test_locator_rejects_absent_empty_and_ambiguous_quotes(self):
        saved = self.upload(raw="重复重复 aaaa".encode("utf-8"))
        case = case_store.load_case(saved["case_id"])
        for quote in ("不存在", "", " ", "重复", "aaa"):
            with self.subTest(quote=quote), self.assertRaises(AppError):
                locate_source_ref(case.case_id, case.files[0], quote)

    def test_locator_never_reads_failed_file_as_evidence(self):
        saved = self.upload("broken.docx", b"invalid")
        case = case_store.load_case(saved["case_id"])
        with self.assertRaises(AppError):
            locate_source_ref(case.case_id, case.files[0], "invalid")

    def test_wrong_quote_and_file_id_are_reported_without_partial_acceptance(self):
        case_id, value = self.gold_validation_input()
        payload = value.model_dump(mode="json")
        first = payload["extraction"]["info_units"][0]["source_refs"][0]
        first["quote"] = "错" * len(first["quote"])
        payload["extraction"]["info_units"][1]["source_refs"][0]["file_id"] = "not-in-case"
        invalid = ValidationInput.model_validate(payload)
        result = validate_extraction(case_id, invalid)
        self.assertFalse(result.valid)
        self.assertIsNone(result.data)
        self.assertEqual(len(result.errors), 2)
        self.assertTrue(all(e.item_id and e.field.startswith("source_refs.") for e in result.errors))

    def test_mismatched_source_kind_is_rejected_against_casefile_metadata(self):
        case_id, value = self.gold_validation_input()
        value.extraction.info_units[0].source_refs[0].source_kind=SourceKind.other
        self.assertFalse(validate_extraction(case_id, value).valid)

    def test_missing_cache_is_a_validation_error(self):
        case_id, value = self.gold_validation_input()
        for file in value.parsed_files:
            case_store.artifact_path(case_id, file.parsed_text_path).unlink()
        result = validate_extraction(case_id, value)
        self.assertFalse(result.valid)
        self.assertIsNone(result.data)
        self.assertTrue(result.errors)

    def test_gold_relations_pass_and_unknown_reference_fails(self):
        _, value = self.gold_validation_input()
        context = RelationInput(**value.extraction.model_dump(mode="json"))
        output = RelationOutput(relations=gold_lines("gold_relations.jsonl"))
        self.assertTrue(validate_relations(output, context).valid)
        payload = output.model_dump(mode="json")
        payload["relations"][0]["target_id"] = "unknown"
        result = validate_relations(RelationOutput(**payload), context)
        self.assertFalse(result.valid)
        self.assertIsNone(result.data)

    def test_persistence_rejects_tampered_quote_before_writing(self):
        case_id, value = self.gold_validation_input()
        from app.case_service import create_empty_case
        case = create_empty_case("案件 A")
        case.case_id = case_id
        case.files = value.parsed_files
        case.info_units = value.extraction.info_units
        case.core_issue = value.extraction.core_issue
        case.issue_set = value.extraction.issue_set
        case_store.save_case(case)
        before = (self.data_dir / case_id / "case.json").read_bytes()
        ref = case.info_units[0].source_refs[0]
        ref.quote = "错" * len(ref.quote)
        with self.assertRaises(AppError):
            case_store.save_case(case)
        self.assertEqual((self.data_dir / case_id / "case.json").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
