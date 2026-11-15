"""Event time follows the existing extraction and InfoUnit write paths; no paid calls."""
import json

from pydantic import ValidationError

from support import IsolatedCaseTest, model_result, model_unit, quote_id_for
import test_extraction_api as api_fixture
from app import case_store
from app.extraction import (
    ExtractionFailure, ModelExtraction, materialize_extraction, parse_model_extraction, validate_extraction_candidates,
)
from app.schemas import InfoUnit


class TestEventTime(IsolatedCaseTest):
    def test_model_schema_requires_nullable_event_time_for_strict_output(self):
        schema = ModelExtraction.model_json_schema()["$defs"]
        self.assertIn("event_time", schema["ModelEvent"]["required"])
        self.assertEqual(schema["ModelEvent"]["properties"]["event_time"]["anyOf"],
                         [{"type": "string"}, {"type": "null"}])
        for kind in ("ModelEntity", "ModelState", "ModelRule", "ModelPosition"):
            self.assertNotIn("event_time", schema[kind]["properties"])

    def fixture(self, raw):
        saved = self.upload(raw=raw.encode(), source_kind="testimony")
        self.case = case_store.load_case(saved["case_id"])
        self.fid = self.case.files[0].file_id
        self.base = f"/api/cases/{self.case.case_id}/info-units"
        return saved

    def event(self, quote, **changes):
        return model_unit(self.fid, quote_id_for(self.case.case_id, self.fid, quote), **changes)

    def create(self, **changes):
        value = dict(type="event", subtype="reported", title="付款陈述",
            content="甲称2026年3月5日已付款。", event_time="2026-03-05",
            evidence_ids=[quote_id_for(self.case.case_id, self.fid, "甲称2026年3月5日已付款。")])
        value.update(changes)
        response = self.client.post(self.base, json=value)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["case"]["info_units"][-1]

    def test_precisions_materialize_and_round_trip_without_changing_truth(self):
        for quote, event_time in [
            ("甲称2026年3月5日已付款。", "2026-03-05"),
            ("甲称2026年3月已付款。", "2026-03"),
            ("甲称2026年已付款。", "2026"),
            ("甲称2026年3月5日至2026年3月7日期间付款。", "2026-03-05/2026-03-07"),
            ("甲称近日已付款。", None),
            ("甲称3月5日已付款。", None),
        ]:
            with self.subTest(event_time=event_time, quote=quote):
                self.fixture(quote)
                for subtype in ("reported", "documented"):
                    draft = parse_model_extraction(model_result([
                        self.event(quote, content=quote, event_time=event_time, subtype=subtype)]))
                    result, _ = materialize_extraction(draft, self.case)
                    self.case.info_units = result.info_units
                    self.case.core_issue, self.case.issue_set = result.core_issue, result.issue_set
                    case_store.save_case(self.case)
                    stored = case_store.load_case(self.case.case_id).info_units[-1]
                    self.assertEqual(stored.event_time, event_time)
                    self.assertEqual(stored.subtype, subtype)
                    self.assertEqual(stored.content, quote)

    def test_invalid_formats_calendar_dates_and_ranges_are_rejected(self):
        self.fixture("甲称2026年3月5日已付款。")
        for value in ("", "近日", "03-05", "2026-3-5", "0000", "2026-13", "2026-02-29",
                      "2026-03-07/2026-03-05", "2026/2027-03-05", "2026-03-05T12:00:00"):
            with self.subTest(value=value), self.assertRaises(ExtractionFailure):
                parse_model_extraction(model_result([self.event(
                    "甲称2026年3月5日已付款。", event_time=value)]))

    def test_time_only_allowed_on_events(self):
        self.fixture("甲称2026年3月5日已付款。")
        unit = self.create()
        for kind in ("entity", "state", "rule", "position"):
            values = {**unit, "type": kind, "subtype": "person" if kind == "entity" else None, "speaker": "甲"}
            with self.subTest(kind=kind), self.assertRaises(ValidationError):
                InfoUnit.model_validate(values)

    def test_missing_year_or_uncited_date_cannot_support_normalized_time(self):
        self.fixture("甲称3月5日已付款。\n2026年3月5日")
        raw = json.loads(model_result([self.event("甲称3月5日已付款。", event_time="2026-03-05")]))
        report = validate_extraction_candidates(raw, self.case)
        self.assertEqual(report.errors[0]["field_path"], "event_time")
        with self.assertRaises(ExtractionFailure):
            materialize_extraction(parse_model_extraction(json.dumps(raw)), self.case)

    def test_multiple_quote_units_supply_event_and_time(self):
        self.fixture("2026年3月5日\n甲称当日已付款。")
        event = self.event("甲称当日已付款。", content="甲称2026年3月5日已付款。", event_time="2026-03-05")
        raw = json.loads(model_result([event]))
        self.assertTrue(validate_extraction_candidates(raw, self.case).errors)
        event["evidence_ids"].append(quote_id_for(self.case.case_id, self.fid, "2026年3月5日"))
        raw = json.loads(model_result([event]))
        self.assertFalse(validate_extraction_candidates(raw, self.case).errors)
        result, _ = materialize_extraction(parse_model_extraction(json.dumps(raw)), self.case)
        self.assertEqual(result.info_units[0].event_time, "2026-03-05")
        self.assertEqual(len(result.info_units[0].source_refs), 2)

    def test_event_date_differs_from_document_and_statement_date(self):
        quote = "文件日期2026年4月1日，甲于当日陈述：2026年3月5日已付款。"
        self.fixture(quote)
        result, _ = materialize_extraction(parse_model_extraction(model_result([
            self.event(quote, content="甲称2026年3月5日已付款。", event_time="2026-03-05")
        ])), self.case)
        self.assertEqual(result.info_units[0].event_time, "2026-03-05")
        self.assertEqual(result.info_units[0].subtype, "reported")

    def test_incremental_extraction_keeps_times_and_merges_only_equal_times(self):
        quote = "甲称2026年3月5日已付款。"
        saved = self.fixture(quote)
        raw = model_result([self.event(quote, content=quote, event_time="2026-03-05")])
        response, call = api_fixture.TestExtractionAPI.run_with_reply(self, saved, raw)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(call.await_count, 1)
        first = case_store.load_case(self.case.case_id).info_units[0]
        added = self.client.post(f"/api/cases/{self.case.case_id}/files", data={
            "source_kinds": json.dumps({"new.txt": "testimony"})}, files={"files": ("new.txt", quote.encode())})
        self.assertEqual(added.status_code, 200, added.text)
        self.case = case_store.load_case(self.case.case_id)
        self.fid = self.case.files[-1].file_id
        raw = model_result([self.event(quote, content=quote, event_time="2026-03-05")])
        response, call = api_fixture.TestExtractionAPI.run_with_reply(self, added.json()["case"], raw)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(call.await_count, 1)
        stored = case_store.load_case(self.case.case_id)
        self.assertEqual(len(stored.info_units), 1)
        self.assertEqual(stored.info_units[0].id, first.id)
        self.assertEqual(stored.info_units[0].event_time, first.event_time)
        self.assertEqual(len(stored.info_units[0].source_refs), 2)
        result, _ = materialize_extraction(parse_model_extraction(model_result([
            self.event(quote, content=quote, event_time="2026-03")
        ])), stored)
        self.assertEqual([u.event_time for u in result.info_units], ["2026-03-05", "2026-03"])

    def test_edit_preserves_stable_fields_and_clears_stale_time(self):
        self.fixture("甲称2026年3月5日已付款。\n乙称未收到款项。")
        first = self.create()
        url = f"{self.base}/{first['id']}"
        response = self.client.patch(url, json={"title": "付款情况陈述"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["case"]["info_units"][0]["event_time"], "2026-03-05")
        for edit in (
            {"content": "甲称已支付另一笔款项。"},
            {"type": "position", "subtype": None, "speaker": "甲"},
            {"evidence_ids": [quote_id_for(self.case.case_id, self.fid, "乙称未收到款项。")]},
        ):
            unit = self.create()
            response = self.client.patch(f"{self.base}/{unit['id']}", json=edit)
            self.assertEqual(response.status_code, 200, response.text)
            stored = response.json()["case"]["info_units"][-1]
            self.assertIsNone(stored["event_time"])
            self.assertEqual((stored["id"], stored["created_at"]), (unit["id"], unit["created_at"]))

    def test_explicit_time_edit_is_validated_and_persisted_atomically(self):
        self.fixture("甲称2026年3月5日已付款。")
        unit = self.create()
        url = f"{self.base}/{unit['id']}"
        for event_time in ("2025-03-05", "2026-02-30"):
            response = self.client.patch(url, json={"event_time": event_time})
            self.assertEqual(response.status_code, 422, response.text)
            self.assertEqual(case_store.load_case(self.case.case_id).info_units[0].event_time, "2026-03-05")
        response = self.client.patch(url, json={"content": "甲称2026年3月曾付款。", "event_time": "2026-03"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(case_store.load_case(self.case.case_id).info_units[0].event_time, "2026-03")
        response = self.client.patch(url, json={"event_time": None})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(case_store.load_case(self.case.case_id).info_units[0].event_time)
