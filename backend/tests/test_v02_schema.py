import copy
import json
import unittest

from pydantic import TypeAdapter, ValidationError

from support import GOLD, gold_lines
from app import case_store
from app.case_service import create_empty_case
from app.errors import AppError
from app.schemas import (
    CaseData, ConflictView, ExtractionOutput, HypothesisView, InfoUnit, LawEntry,
    Relation, RelationInput, SourceRef, TimelineView, ViewPayload, check_relations,
)


def lines(name):
    return gold_lines(name)


class TestV03Schema(unittest.TestCase):
    def setUp(self):
        self.units = lines("gold_info_units.jsonl")
        issues = json.loads((GOLD / "gold/gold_issues.json").read_text(encoding="utf-8"))
        self.extraction = ExtractionOutput(core_issue=issues["core_issue"], issue_set=issues["issue_set"],
                                           info_units=self.units)

    def test_v03_gold_and_relations_obey_contract(self):
        self.assertEqual(len(self.extraction.info_units), 17)
        relations = [Relation.model_validate(row) for row in lines("gold_relations.jsonl")]
        self.assertEqual(len(relations), 7)
        check_relations(self.extraction.info_units, relations)
        self.assertNotIn("summary", self.extraction.info_units[0].model_dump())

    def test_type_subtype_pairs_and_initial_verified_boundary(self):
        base = self.units[0]
        valid = [
            {**base, "type": "entity", "subtype": "object"},
            {**base, "type": "event", "subtype": "reported"},
            {**base, "type": "event", "subtype": "documented"},
            {**base, "type": "event", "subtype": "verified"},
            {**base, "type": "event", "subtype": None},
            {**base, "type": "rule", "subtype": None},
        ]
        for value in valid:
            InfoUnit.model_validate(value)
        for value in ({**base, "type": "statement"}, {**base, "type": "state", "subtype": "reported"},
                      {**base, "type": "entity", "subtype": None}):
            with self.assertRaises(ValidationError):
                InfoUnit.model_validate(value)

    def test_state_and_position_require_real_nonblank_speaker(self):
        base = self.units[2]
        for kind in ("state", "position"):
            for speaker in (None, "", "  "):
                with self.subTest(kind=kind, speaker=speaker), self.assertRaises(ValidationError):
                    InfoUnit.model_validate({**base, "type": kind, "speaker": speaker})
        InfoUnit.model_validate({**base, "speaker": "租房合同"})

    def test_unicode_title_and_content_boundaries_do_not_truncate(self):
        base = self.units[0]
        for title in ("甲" * 2, "😀" * 24):
            self.assertEqual(InfoUnit.model_validate({**base, "title": title}).title, title)
        for content in ("甲" * 4, "😀" * 220):
            self.assertEqual(InfoUnit.model_validate({**base, "content": content}).content, content)
        for field, values in (("title", ["甲", "甲" * 25, " 两字"]),
                              ("content", ["甲" * 3, "甲" * 221, " 四个字"])):
            for value in values:
                with self.subTest(field=field, length=len(value)), self.assertRaises(ValidationError):
                    InfoUnit.model_validate({**base, field: value})

    def test_related_issue_and_entity_id_references(self):
        payload = self.extraction.model_dump(mode="json")
        payload["info_units"][5]["actor"] = "张某"
        with self.assertRaises(ValidationError):
            ExtractionOutput.model_validate(payload)
        payload = self.extraction.model_dump(mode="json")
        payload["info_units"][5]["actor"] = "U004"
        with self.assertRaises(ValidationError):
            ExtractionOutput.model_validate(payload)
        payload = self.extraction.model_dump(mode="json")
        payload["info_units"][5]["related_issue"] = "未登记争点"
        with self.assertRaises(ValidationError):
            ExtractionOutput.model_validate(payload)

    def test_relation_endpoints_evidence_and_types(self):
        relations = [Relation.model_validate(row) for row in lines("gold_relations.jsonl")]
        for change in ({"target_id": "missing"}, {"evidence_unit_ids": ["missing"]},
                       {"relation_type": "actor_of"}):
            payload = relations[0].model_dump(mode="json") | change
            with self.assertRaises((ValidationError, ValueError)):
                relation = Relation.model_validate(payload)
                check_relations(self.extraction.info_units, [relation])

    def test_all_three_view_payloads_and_claim_role(self):
        cases = lines("gold_view_cases.jsonl")
        payloads = [row["expected"]["reference_payload"] for row in cases]
        self.assertEqual({payload["view_type"] for payload in payloads}, {"conflict", "timeline", "hypothesis"})
        for payload in payloads:
            TypeAdapter(ViewPayload).validate_python(payload)
        conflict = ConflictView.model_validate(next(
            payload for payload in payloads if payload["view_type"] == "conflict"))
        self.assertEqual(conflict.sections[0].items[0].role, "claim")
        self.assertNotEqual(self.extraction.info_units[0].type.value, "statement")

    def test_hypothesis_dependency_and_timeline_refs(self):
        with self.assertRaises(ValidationError):
            HypothesisView(view_type="hypothesis", title="测试假设", hypothesis="待验证假设", missing_information=[], steps=[
                {"id":"a","claim":"第一步骤","supporting_info_unit_ids":[],"opposing_info_unit_ids":[],"depends_on_step_ids":["a"]}])
        timeline = TimelineView.model_validate(next(
            row["expected"]["reference_payload"] for row in lines("gold_view_cases.jsonl")
            if row["expected"]["view_type"] == "timeline"))
        self.assertIsNone(timeline.events[-1].sort_date)

    def test_source_unicode_offsets(self):
        source = dict(file_id="file-a", source_kind="document", quote="😀甲", start=1, end=3)
        self.assertEqual(SourceRef(**source).end, 3)
        for change in ({"start": True}, {"start": "1"}, {"start": -1}, {"end": 4}, {"end": 1}):
            with self.assertRaises(ValidationError):
                SourceRef(**(source | change))

    def test_workspace_references_and_law_contract(self):
        case = create_empty_case("测试").model_dump(mode="json")
        case["workspace_state"]["selected_node_ids"] = ["missing"]
        with self.assertRaises(ValidationError):
            CaseData.model_validate(case)
        law = dict(id="LAW-a", title="法规", article_no="第一条", text="原文", source_url="https://example.com",
                   effective_status="effective", verified_at=None)
        self.assertEqual(LawEntry(**law).text, "原文")

    def test_relation_input_preserves_v03_fields(self):
        value = RelationInput(**self.extraction.model_dump(mode="json"))
        self.assertEqual(value.info_units, self.extraction.info_units)

    def test_legacy_migration_maps_unique_names_and_reports_ambiguity(self):
        base = create_empty_case("旧案").model_dump(mode="json")
        entity = copy.deepcopy(self.units[0])
        event = copy.deepcopy(self.units[5])
        event.update({"subtype": "transfer", "summary": "旧摘要", "actor": "李某", "target": None})
        event["stance_target"] = event.pop("related_issue")
        base["info_units"] = [entity, event]
        migrated = case_store.migrate_v02_snapshot(base)
        self.assertEqual(migrated["info_units"][1]["actor"], "U001")
        self.assertNotIn("summary", migrated["info_units"][1])
        ambiguous = copy.deepcopy(base)
        statement = copy.deepcopy(event)
        statement.update({"id":"legacy-statement", "type":"statement", "subtype":"fact_assertion"})
        ambiguous["info_units"].append(statement)
        with self.assertRaises(AppError) as caught:
            case_store.migrate_v02_snapshot(ambiguous)
        self.assertEqual(caught.exception.code, "legacy_migration_ambiguous")


if __name__ == "__main__":
    unittest.main()
