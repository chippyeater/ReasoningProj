"""Offline quality metrics and experimental adapters; no semantic acceptance test."""
from copy import deepcopy
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO), str(REPO / "backend")]
from app import investigation_tools as tools, llm_client
from evals.case_a.investigation.data import build_case, digest
from evals.case_a.retrieval import evaluate as ev


class TestRetrievalEval(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.case = build_case()
        cls.definition = ev.load_queries()

    def test_new_labels_are_separate_pending_and_case_scoped(self):
        self.assertEqual(self.definition["annotation_status"], "requires_human_review")
        self.assertEqual(len(self.definition["queries"]), 26)
        self.assertEqual(sum(q["aggregate"] for q in self.definition["queries"]), 25)
        self.assertEqual(set(self.definition["needs"]), {"a1", "loss", "rent", "departure", "arrival", "lease", "vacancy", "deposit"})
        self.assertEqual(self.definition["needs"]["deposit"]["relevant_ids"], ["U006", "U008"])
        self.assertEqual(self.definition["needs"]["deposit"]["context_ids"], ["U010", "U019"])
        self.assertFalse({"rule_filter", "source_only", "negative", "wrong_type_empty", "punctuation", "blank", "duplicate_terms", "nfkc_number"}
                         & {q["id"] for q in self.definition["queries"]})
        for need in self.definition["needs"].values():
            self.assertLess(len(need["relevant_ids"]), len(self.case.info_units))
        bad = deepcopy(self.definition)
        bad["needs"]["rent"]["relevant_ids"] = ["outside-case"]
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text(json.dumps(bad), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "current-snapshot"):
                ev.load_queries(path)

    def test_metrics_distinguish_rank_cutoff_missing_and_pagination(self):
        ranking = [{"id": f"u{i}"} for i in range(1, 10)]
        row = ev.metrics(ranking, ["u1", "u7", "absent"])
        self.assertEqual(row["recall_at_5"], 1 / 3)
        self.assertEqual(row["recall_at_8"], 2 / 3)
        self.assertEqual(row["precision_at_5"], 1 / 5)
        self.assertEqual(row["precision_at_8"], 2 / 8)
        self.assertEqual(row["missed_ids"], ["absent"])
        self.assertEqual(row["relevant_beyond_5"], ["u7"])
        self.assertEqual(row["pagination_5"]["calls_to_exhaust"], 2)
        self.assertEqual(row["pagination_5"]["relevant_first_page"], {"u1": 1, "u7": 2, "absent": None})
        self.assertEqual(row["query_coverage"], 2 / 3)

    def test_short_results_and_empty_labels_are_not_silently_perfect_precision(self):
        row = ev.metrics([{"id": "u"}], ["u"])
        self.assertEqual(row["precision_at_5"], 0.2)
        self.assertEqual(row["returned_precision_at_5"], 1)
        empty = ev.metrics([], [])
        self.assertIsNone(empty["recall_at_5"])
        self.assertIsNone(empty["returned_precision_at_5"])

    def test_full_diagnostic_ranking_matches_actual_bounded_production_on_every_query(self):
        for query in self.definition["queries"]:
            with self.subTest(query=query["id"]):
                ranked = ev.rank(self.case, query["query"], query["types"])
                for k in (5, 8):
                    actual = tools.search_info_units(self.case, query["query"], query["types"], k)
                    self.assertEqual([r["id"] for r in ranked["ranking"][:k]], [r["id"] for r in actual["items"]])
                    self.assertEqual(ranked["error"], actual["error"])
                    self.assertEqual(len(ranked["ranking"]) > k, actual["has_more"])

    def test_original_v15_has_14_hits_and_separates_unmatched_from_below_limit(self):
        recorded = ev.verify_recorded_call(self.case, self.definition)
        self.assertEqual(len(recorded["full_ranking"]["ranking"]), 14)
        expected = {"U004": 13, "U022": None, "U026": None, "U027": 6, "U029": 9, "U035": 14, "U036": 10}
        self.assertEqual({uid: row["rank"] for uid, row in recorded["key_units"].items()}, expected)
        self.assertEqual(recorded["key_units"]["U004"]["reason"], "below_limit")
        self.assertEqual(recorded["key_units"]["U022"]["reason"], "no_indexed_keyword_overlap")
        self.assertFalse(any(recorded["content_truncated"]))

    def test_original_audit_is_verified_without_requiring_ignored_live_files(self):
        original = self.definition["original_call"]
        call = {"name": "search_info_units", "call_id": original["call_id"],
            "arguments": {key: original[key] for key in ("query", "types", "limit")},
            "result": tools.search_info_units(self.case, original["query"], original["types"], original["limit"])}
        audit = {"run_id": original["run_id"], "tool_calls": [call],
            "result": {"tool_budget_used": {"read": 1}, "limitations": []}}
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "audit.json"
            path.write_text(json.dumps(audit), encoding="utf-8")
            self.assertTrue(ev.verify_recorded_call(self.case, self.definition, path)["audit"]["local_audit_verified"])
            audit["tool_calls"][0]["arguments"]["limit"] = 8
            path.write_text(json.dumps(audit), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "differ"):
                ev.verify_recorded_call(self.case, self.definition, path)

    def test_bigram_parser_is_generic_and_cannot_solve_pure_synonyms(self):
        self.assertEqual(ev.literal_terms("合同租金是多少"), ["合同租金是多少"])
        self.assertEqual(ev.bigrams("月租是多少"), ["月租", "租是", "是多", "多少"])
        self.assertEqual(ev.bigrams("ＡＢＣ ３０００"), ["abc", "3000"])
        self.assertFalse(ev.rank(self.case, "合同约定每个月要交多少租金")["ranking"])
        self.assertIn("U004", {r["id"] for r in ev.rank(self.case, "合同约定每个月要交多少租金", strategy="bigram_coverage")["ranking"]})
        self.assertFalse(ev.rank(self.case, "房租", strategy="bigram_coverage")["ranking"])

    def test_and_requires_every_literal_term_and_is_not_default(self):
        original = self.definition["original_call"]
        self.assertTrue(ev.rank(self.case, original["query"])["ranking"])
        self.assertFalse(ev.rank(self.case, original["query"], strategy="literal_and")["ranking"])
        rows = ev.rank(self.case, "合同 押金", strategy="literal_and")["ranking"]
        self.assertTrue(rows)
        self.assertTrue(all(r["coverage"] == 2 for r in rows))

    def test_experiments_keep_type_filters_case_immutable_and_stable_ties(self):
        before = self.case.model_dump_json()
        reversed_case = self.case.model_copy(deep=True)
        reversed_case.info_units.reverse()
        for strategy in ev.STRATEGIES:
            with self.subTest(strategy=strategy):
                first = ev.rank(self.case, "押金", ["rule"], strategy)
                second = ev.rank(reversed_case, "押金", ["rule"], strategy)
                self.assertEqual(first, second)
                self.assertEqual([r["id"] for r in first["ranking"]], ["U008"])
        self.assertEqual(self.case.model_dump_json(), before)

    def test_evaluation_is_deterministic_and_never_calls_model_or_network(self):
        before = digest(self.definition)
        with patch.object(llm_client, "call_tools") as model, patch.object(llm_client, "_post_completion") as transport, \
                patch("socket.create_connection", side_effect=AssertionError("Network forbidden")) as network:
            one, two = ev.evaluate(), ev.evaluate()
        self.assertEqual(one, two)
        model.assert_not_called()
        transport.assert_not_called()
        network.assert_not_called()
        self.assertEqual(digest(self.definition), before)
        self.assertEqual(one["paid_model_calls"], 0)
        self.assertFalse(one["manual_a1_plan"]["missed_ids"])
        self.assertTrue(all(v["query_macro"]["query_count"] == 25 for v in one["summary"].values()))

    def test_primary_weights_seven_needs_once_and_reports_a1_separately(self):
        result = ev.evaluate()
        for strategy, summary in result["summary"].items():
            with self.subTest(strategy=strategy):
                self.assertEqual(set(summary["by_need"]), {"loss", "rent", "departure", "arrival", "lease", "vacancy", "deposit"})
                self.assertEqual(summary["by_need"]["deposit"]["query_count"], 2)
                self.assertEqual(summary["query_macro"]["query_count"], 25)
                self.assertNotIn("recorded_broad", summary["by_kind"])
                for name in ("recall_at_5", "recall_at_8", "precision_at_5", "precision_at_8", "query_coverage"):
                    self.assertAlmostEqual(summary["primary_need_macro"][name], sum(v[name] for v in summary["by_need"].values()) / 7)
        production = result["summary"]["production"]
        self.assertNotAlmostEqual(production["primary_need_macro"]["recall_at_5"], production["query_macro"]["recall_at_5"])
        self.assertEqual(production["a1"]["query_id"], "v15")
        self.assertEqual(production["a1"]["recall_at_5"], 0)
        self.assertEqual(production["a1"]["recall_at_8"], 1 / 7)
        self.assertEqual(production["a1"]["missed_ids"], ["U022", "U026"])
        report = ev.render_report(result)
        self.assertIn("7项单项信息需求、25条Query", report)
        self.assertIn("A1综合调查目标（不计入主平均）", report)
        self.assertIn("新旧总体指标不可直接比较", report)

    def test_a1_label_change_does_not_affect_primary_metrics_or_enter_aggregate(self):
        baseline = ev.evaluate()
        changed = deepcopy(self.definition)
        changed["needs"]["a1"]["relevant_ids"] = ["U004"]
        altered = ev.evaluate(changed)
        self.assertNotEqual(baseline["summary"]["production"]["a1"]["recall_at_8"], altered["summary"]["production"]["a1"]["recall_at_8"])
        for strategy in ev.STRATEGIES:
            self.assertEqual(baseline["summary"][strategy]["primary_need_macro"], altered["summary"][strategy]["primary_need_macro"])
        changed["queries"][0]["aggregate"] = True
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "mixed.json"
            path.write_text(json.dumps(changed), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "A1 is separate"):
                ev.load_queries(path)

    def test_cli_refuses_to_overwrite_existing_outputs_before_evaluating(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "existing.json"
            path.write_text("preserved", encoding="utf-8")
            with patch.object(ev, "evaluate") as run, patch("sys.stderr"), self.assertRaises(SystemExit):
                ev.main(["--output", str(path)])
            run.assert_not_called()
            self.assertEqual(path.read_text(encoding="utf-8"), "preserved")


if __name__ == "__main__":
    unittest.main()
