import copy
import json
import re
import unittest

from support import GOLD, REPO, gold_lines
from pydantic import TypeAdapter, ValidationError
from app import schemas
from app.case_service import create_empty_case
from app.schemas import (
    CaseData, ConflictView, ExtractionOutput, HypothesisView, InfoUnit, LawEntry,
    Relation, RelationInput, SourceRef, ViewPayload, check_relations,
)


class TestV02Schema(unittest.TestCase):
    def setUp(self):
        self.units = gold_lines("gold_info_units.jsonl")
        issues = json.loads((GOLD / "gold/gold_issues.json").read_text(encoding="utf-8"))
        self.extraction = ExtractionOutput(core_issue=issues["core_issue"], issue_set=issues["issue_set"],
                                           info_units=self.units)

    def test_every_named_target_contract_is_present(self):
        document = (REPO / "LogicWeaver｜最小 MVP 数据 Schema v0.2.md").read_text(encoding="utf-8")
        names = set(re.findall(r"^type (\w+)", document, re.M))
        self.assertGreater(len(names), 30)
        self.assertEqual(names - set(vars(schemas)), set())
        for name in names:
            with self.subTest(name=name):
                TypeAdapter(getattr(schemas, name)).json_schema()

    def test_all_gold_units_and_relations_obey_new_contract(self):
        self.assertEqual(len(self.extraction.info_units), 16)
        relations = [Relation.model_validate(r) for r in gold_lines("gold_relations.jsonl")]
        self.assertEqual(len(relations), 9)
        check_relations(self.extraction.info_units, relations)

    def test_unknown_fields_and_missing_required_fields_are_rejected(self):
        for change in ({"unexpected_field": "invalid"}, {"content": None}):
            value = {**self.units[0], **change}
            with self.subTest(change=change), self.assertRaises(ValidationError):
                InfoUnit.model_validate(value)
        value = dict(self.units[0])
        del value["speaker"]
        with self.assertRaises(ValidationError):
            InfoUnit.model_validate(value)

    def test_invalid_classifications_and_empty_values_are_rejected(self):
        for change in ({"type": "unknown"}, {"type": "entity", "subtype": "transfer"},
                       {"content": "  "}, {"source_refs": []}):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                InfoUnit.model_validate({**self.units[0], **change})

    def test_role_and_speaker_are_not_erased_for_events(self):
        value = {**self.units[0], "type": "event", "subtype": "action",
                 "speaker": "陈述人", "side": "第三人", "actor": "甲", "target": "乙"}
        unit = InfoUnit.model_validate(value)
        self.assertEqual(unit.speaker, "陈述人")
        self.assertEqual(unit.side, "第三人")
        self.assertEqual(unit.actor, "甲")

    def test_unicode_offsets_require_integers_and_correct_lengths(self):
        source = dict(file_id="file-a", source_kind="document", quote="😀甲", start=1, end=3)
        self.assertEqual(SourceRef(**source).end, 3)
        for change in ({"start": True}, {"start": "1"}, {"start": -1}, {"end": 4}, {"end": 1}):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                SourceRef(**{**source, **change})

    def test_duplicate_units_and_unregistered_issues_are_rejected(self):
        base = self.extraction.model_dump(mode="json")
        for change in ({"core_issue": "未定义争点"}, {"info_units": [self.units[0], self.units[0]]},
                       {"issue_set": ["重复", "重复"]}):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                ExtractionOutput(**{**base, **change})
        invalid = copy.deepcopy(base)
        invalid["info_units"][0]["stance_target"] = "未定义争点"
        with self.assertRaises(ValidationError):
            ExtractionOutput(**invalid)

    def test_relation_endpoints_and_evidence_must_exist(self):
        relations = [Relation.model_validate(r) for r in gold_lines("gold_relations.jsonl")]
        invalid = relations[0].model_copy(update={"evidence_unit_ids": ["missing"]})
        with self.assertRaises(ValueError):
            check_relations(self.extraction.info_units, [invalid])
        invalid = relations[0].model_copy(update={"source_id": "missing"})
        with self.assertRaises(ValueError):
            check_relations(self.extraction.info_units, [invalid])

    def test_occur_at_requires_temporal_state(self):
        event = InfoUnit.model_validate({**self.units[0], "id": "event-a", "type": "event", "subtype": "action"})
        quantity = InfoUnit.model_validate({**self.units[0], "id": "state-a", "type": "state", "subtype": "quantitative"})
        relation = Relation(id="r", source_id=event.id, target_id=quantity.id, relation_type="occur_at",
                            evidence_unit_ids=[event.id], rationale="依据", certainty="explicit")
        with self.assertRaises(ValueError):
            check_relations([event, quantity], [relation])

    def test_hypothesis_cycles_and_unknown_dependencies_fail(self):
        base = dict(view_type="hypothesis", title="假设", hypothesis="待验证", missing_information=[])
        def step(sid, deps):
            return dict(id=sid, claim="推理", supporting_info_unit_ids=[], opposing_info_unit_ids=[],
                        depends_on_step_ids=deps)
        for steps in ([step("a", ["b"]), step("b", ["a"])], [step("a", ["missing"])]):
            with self.subTest(steps=steps), self.assertRaises(ValidationError):
                HypothesisView(**base, steps=steps)
        self.assertEqual(len(HypothesisView(**base, steps=[step("a", []), step("b", ["a"])]).steps), 2)

    def test_view_union_does_not_accept_unstructured_payloads(self):
        with self.assertRaises(ValidationError):
            TypeAdapter(ViewPayload).validate_python({"view_type": "unknown", "title": "无效"})
        with self.assertRaises(ValidationError):
            ConflictView(view_type="conflict", title="争点", missing_information=[], sections=[
                {"id": "s", "title": "一方", "placement": "left", "items": [
                    {"info_unit_id": "u", "role": "claim", "emphasis": "high", "default_expanded": False}]}])

    def test_case_rejects_unknown_workspace_nodes(self):
        case = create_empty_case("测试").model_dump(mode="json")
        case["workspace_state"]["selected_node_ids"] = ["missing"]
        with self.assertRaises(ValidationError):
            CaseData.model_validate(case)

    def test_law_model_requires_verification_metadata(self):
        data = dict(id="LAW-a", title="法规", article_no="第一条", text="原文", source_url="https://example.com",
                    effective_status="effective", verified_at=None)
        self.assertEqual(LawEntry(**data).text, "原文")
        with self.assertRaises(ValidationError):
            LawEntry(**{**data, "effective_status": "有效"})
        del data["verified_at"]
        with self.assertRaises(ValidationError):
            LawEntry(**data)

    def test_entire_runtime_law_corpus_obeys_target_contract(self):
        entries=json.loads((REPO / 'backend/data/laws.json').read_text(encoding='utf-8'))
        self.assertEqual(len(entries),19)
        for entry in entries:
            with self.subTest(id=entry['id']):
                LawEntry.model_validate(entry)

    def test_all_gold_view_payloads_obey_target_contract(self):
        cases=gold_lines('gold_view_cases.jsonl')
        self.assertEqual(len(cases),6)
        for case in cases:
            with self.subTest(id=case['id']):
                TypeAdapter(ViewPayload).validate_python(case['expected']['reference_payload'])

    def test_relation_input_preserves_all_new_unit_fields(self):
        value = RelationInput(**self.extraction.model_dump(mode="json"))
        self.assertEqual(value.info_units, self.extraction.info_units)


if __name__ == "__main__":
    unittest.main()
