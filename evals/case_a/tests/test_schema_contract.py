"""Check Gold v1.7 against its frozen v0.2 contract offline."""
import sys
import unittest

from validate_eval import ROOT, jl, js, validate_task_references

sys.path.insert(0, str(ROOT.parents[1] / 'backend'))
from app.schemas import ConflictView, HypothesisView, TimelineView
from legacy_v0_2_contract import ExtractionOutput, Relation, check_relations


class TestGoldSchemaContract(unittest.TestCase):
    def test_extraction_and_relation_models(self):
        issues = js('gold/gold_issues.json')
        output = ExtractionOutput.model_validate({
            'core_issue': issues['core_issue'], 'issue_set': issues['issue_set'],
            'info_units': jl('gold_info_units.jsonl')})
        relations = [Relation.model_validate(x) for x in jl('gold_relations.jsonl')]
        check_relations(output.info_units, relations)

    def test_all_reference_view_payloads(self):
        models = {'timeline': TimelineView, 'conflict': ConflictView, 'hypothesis': HypothesisView}
        for case in jl('gold_view_cases.jsonl'):
            with self.subTest(case=case['id']):
                payload = case['expected']['reference_payload']
                models[payload['view_type']].model_validate(payload)

    def test_nested_task_input_and_expectation_stale_ids_are_rejected(self):
        namespace = {'selected_info_unit_ids': {u['id'] for u in jl('gold_info_units.jsonl')}}
        with self.assertRaisesRegex(AssertionError, 'unknown references'):
            validate_task_references({'input': {'selected_info_unit_ids': ['U999']}}, namespace, 'task')


if __name__ == '__main__':
    unittest.main()
