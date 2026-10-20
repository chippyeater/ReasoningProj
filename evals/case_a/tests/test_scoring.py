"""Offline scorer regressions using test data, never model calls or semantic scores."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from gold_basis import current_gold_basis
from score_law_retrieval import evaluate as score_laws
from score_view_selection import score as score_views
from validate_eval import ROOT, jl, js


class TestCurrentGoldScoring(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.basis = current_gold_basis()
        self.units = jl('gold_info_units.jsonl')
        self.prediction = {k: v for k, v in js('gold/gold_issues.json').items()
                           if k in {'core_issue', 'issue_set'}}
        self.prediction['info_units'] = copy.deepcopy(self.units)
        self.rows = [dict(gold_version=self.basis['version'], gold_basis_sha256=self.basis['sha256'],
                          gold_id=u['id'], predicted_id=u['id'], match='yes',
                          review_comment='Scorer self-check with identical test data; not a model evaluation.')
                     for u in self.units]

    def score(self, rows=None, prediction=None):
        pred = self.directory / 'prediction.json'
        alignment = self.directory / 'alignment.jsonl'
        # Each assertion gets its own fresh output; the CLI protects existing scores.
        output = self.directory / f'score-{len(list(self.directory.glob("score-*.json")))}.json'
        pred.write_text(json.dumps(self.prediction if prediction is None else prediction), encoding='utf-8')
        alignment.write_text(''.join(json.dumps(x) + '\n' for x in (self.rows if rows is None else rows)), encoding='utf-8')
        before = pred.read_bytes()
        process = subprocess.run([sys.executable, '-X', 'utf8', '-B', str(ROOT / 'tests/score_predictions.py'),
                                  '--prediction', str(pred), '--alignment', str(alignment),
                                  '--output', str(output)], capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(pred.read_bytes(), before, 'Scoring must not mutate predictions')
        return process, json.loads(output.read_text(encoding='utf-8')) if process.returncode == 0 else None

    def test_all_current_gold_including_independent_lease_and_composites(self):
        self.rows.reverse()
        self.prediction['info_units'].reverse()
        process, result = self.score()
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(result['reviewed_matches'], len(self.units))
        self.assertEqual(result['gold_coverage'], 1)
        self.assertEqual(result['reference_invalid'], 0)
        self.assertEqual(result['gold_basis'], self.basis)
        self.assertEqual(result['prediction_sha256'], hashlib.sha256(
            (self.directory / 'prediction.json').read_bytes()).hexdigest())

    def test_one_prediction_cannot_score_both_move_in_and_new_lease(self):
        next(x for x in self.rows if x['gold_id'] == 'U027')['predicted_id'] = 'U026'
        process, _ = self.score()
        self.assertNotEqual(process.returncode, 0)
        self.assertIn('at most one Gold', process.stderr)

    def test_missing_new_gold_row_is_rejected(self):
        process, _ = self.score(rows=[x for x in self.rows if x['gold_id'] != 'U027'])
        self.assertNotEqual(process.returncode, 0)
        self.assertIn('missing=', process.stderr)
        self.assertIn('U027', process.stderr)

    def test_old_version_or_stale_basis_is_rejected(self):
        for key, value in [('gold_version', '1.5'), ('gold_basis_sha256', '0' * 64)]:
            with self.subTest(key=key):
                rows = copy.deepcopy(self.rows)
                rows[0][key] = value
                process, _ = self.score(rows=rows)
                self.assertNotEqual(process.returncode, 0)
                self.assertIn('version/hash mismatch', process.stderr)

    def test_unreviewed_template_cannot_be_presented_as_score(self):
        rows = [json.loads(line) for line in (ROOT / 'tests/alignment_template.jsonl').read_text(
            encoding='utf-8').splitlines() if line.strip()]
        process, _ = self.score(rows=rows)
        self.assertNotEqual(process.returncode, 0)
        self.assertIn('review incomplete', process.stderr)

    def test_duplicate_or_unknown_gold_rows_are_rejected(self):
        for replacement in ['U001', 'U999']:
            with self.subTest(gold_id=replacement):
                rows = copy.deepcopy(self.rows)
                rows[-1]['gold_id'] = replacement
                process, _ = self.score(rows=rows)
                self.assertNotEqual(process.returncode, 0)

    def test_existing_score_is_not_overwritten(self):
        path = self.directory / 'existing.json'
        path.write_text('historical score', encoding='utf-8')
        process = subprocess.run([sys.executable, '-X', 'utf8', '-B', str(ROOT / 'tests/score_predictions.py'),
                                  '--prediction', 'unused', '--alignment', 'unused', '--output', str(path)],
                                 capture_output=True, text=True, encoding='utf-8')
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual(path.read_text(encoding='utf-8'), 'historical score')

    def test_invalid_sources_are_reported_without_changing_match_standard(self):
        mutations = [dict(start=-10, end=-1), dict(start=True), dict(file_id='missing'),
                     dict(source_kind='testimony'), dict(quote='unsupported quotation'), dict(end=9999)]
        for mutation in mutations:
            with self.subTest(source=mutation):
                prediction = copy.deepcopy(self.prediction)
                prediction['info_units'][0]['source_refs'][0].update(mutation)
                process, result = self.score(prediction=prediction)
                self.assertEqual(process.returncode, 0, process.stderr)
                self.assertEqual(result['reference_invalid'], 1)

    def test_saved_historical_prediction_can_be_read_with_current_review(self):
        # Optional local archive smoke check; no semantic mappings are invented.
        path = ROOT / 'results/extraction_20261020_first_only_5875b93d/prediction_for_scoring.json'
        if not path.is_file():
            self.skipTest('Historical output is a local, untracked artifact')
        original = path.read_bytes()
        rows = [{**row, 'predicted_id': '', 'match': 'no',
                 'review_comment': 'Transport-only regression; semantic review intentionally not performed.'}
                for row in self.rows]
        process, result = self.score(rows=rows, prediction=json.loads(original))
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(result['gold_units'], self.basis['gold_units'])
        self.assertEqual(result['reference_invalid'], 0)
        self.assertEqual(path.read_bytes(), original)

    def test_law_scorer_accepts_current_new_lease_reference(self):
        law = next(x for x in js('gold/verified_laws.json') if x['id'] == 'LAW-0584')
        prediction = {'id': 'L002', 'matches': [{
            'law_id': law['id'], 'issue': 'Loss assessment', 'related_info_unit_ids': ['U027'],
            'relevance_reason': 'Test reference integrity only; applicability requires review.'}]}
        path = self.directory / 'law.jsonl'
        path.write_text(json.dumps(prediction) + '\n', encoding='utf-8')
        result = score_laws(path)
        self.assertEqual(result['gold_case_count'], len(jl('gold_law_cases.jsonl')))
        self.assertTrue(result['results'][0]['id_checks_pass'])
        prediction['matches'][0]['related_info_unit_ids'] = ['U999']
        path.write_text(json.dumps(prediction) + '\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'invalid InfoUnit reference'):
            score_laws(path)

    def test_view_selector_reads_current_tasks_without_fixed_order(self):
        cases = list(reversed(jl('gold_view_selection.jsonl')))
        predictions = {x['id']: {'view_type': x['expected']['allowed_view_types'][0]} for x in cases}
        result = score_views(predictions)
        self.assertEqual(len(result['records']), len(cases))
        self.assertEqual(result['constrained_accuracy'], 1)
        self.assertEqual(result['flexible_acceptance'], 1)
        self.assertIsNone(result['open_view_type_accuracy'])


if __name__ == '__main__':
    unittest.main()
