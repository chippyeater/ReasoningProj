import unittest
from validate_eval import validate, jl, js

class TestDataset(unittest.TestCase):
    def test_full_dataset_integrity(self):
        counts=validate()
        self.assertEqual(counts['gold_info_units'],17) # 案件 A 精简参考集，非系统输出上限
        self.assertEqual(counts['negative_relations'],10)

    def test_all_keys_cover_core_dispute(self):
        gold={u['id']:u for u in jl('gold_info_units.jsonl')}
        for uid in ['U004','U006','U008','U010','U015','U019','U022','U026','U027','U029','U030','U035','U036']:
            self.assertIn(uid,gold)

    def test_issue_oriented_grouping_and_independent_new_lease(self):
        gold={u['id']:u for u in jl('gold_info_units.jsonl')}
        self.assertEqual(len(gold['U008']['source_refs']),2)
        self.assertIn('退还',gold['U008']['content'])
        self.assertIn('不予退还',gold['U008']['content'])
        self.assertEqual(len(gold['U015']['source_refs']),1)
        self.assertNotIn('看情况再说',gold['U015']['content'])
        self.assertEqual(gold['U019']['type'],'position')
        self.assertEqual(gold['U035']['speaker'],'李某')
        self.assertEqual(len(gold['U035']['source_refs']),2)
        self.assertIn('3月8日',gold['U026']['content'])
        self.assertNotIn('签订',gold['U026']['content'])
        self.assertIn('合同',gold['U027']['content'])
        self.assertIsNone(gold['U027']['actor'])

    def test_missing_chat_preserves_testimony_based_units(self):
        e=next(x for x in jl('gold_end_to_end.jsonl') if x['id']=='E2E003')
        self.assertNotIn('U019',e['expected']['forbidden_info_unit_gold_ids'])
        self.assertNotIn('U035',e['expected']['forbidden_info_unit_gold_ids'])
        self.assertIn('file-16d4ace3c7',e['expected']['forbidden_source_file_ids'])

    def test_open_question_does_not_require_fixed_view(self):
        cases={x['id']:x for x in jl('gold_view_selection.jsonl')}
        self.assertEqual(cases['S009']['expected']['selection_mode'],'open')
        self.assertEqual(set(cases['S009']['expected']['allowed_view_types']),{'timeline','conflict','hypothesis'})
        self.assertEqual(cases['S002']['expected']['selection_mode'],'flexible')

    def test_spc_interpretation_clauses_and_case_scope(self):
        laws={x['id']:x for x in js('gold/verified_laws.json')}
        self.assertTrue({'LAW-SC-0063','LAW-SC-0064','LAW-SC-0065','LAW-SC-0067'}<=set(laws))
        self.assertTrue(all(laws[k]['source_url'].startswith('https://www.court.gov.cn/') for k in ['LAW-SC-0063','LAW-SC-0064','LAW-SC-0065','LAW-SC-0067']))
        cases={c['id']:c for c in jl('gold_law_cases.jsonl')}
        self.assertEqual(cases['L006']['expected']['required_law_ids'],['LAW-SC-0067'])
        self.assertNotIn('LAW-SC-0061',laws)

    def test_vacancy_support_is_not_prohibited(self):
        forbidden={(x['source_id'],x['target_id'],x['relation_type']) for x in jl('negative_relations.jsonl')}
        self.assertNotIn(('U035','U036','support'),forbidden)

if __name__=='__main__':unittest.main()
