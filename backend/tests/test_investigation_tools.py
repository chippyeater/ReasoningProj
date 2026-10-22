"""Offline contract and bounded investigation checks using the active Case A."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend"))

from app import case_store
from app.case_service import create_empty_case
from app.investigation_tools import (
    MAX_EVIDENCE_CHARS, MAX_EVIDENCE_REFS, SUMMARY_CHARS,
    get_evidence, get_relations, search_info_units,
)
from app.schemas import CaseData, InfoUnit, Relation, SourceRef

GOLD = REPO / "evals/case_a"


def gold_lines(name):
    return [json.loads(line) for line in (GOLD / "gold" / name).read_text(encoding="utf-8").splitlines()
            if line.strip()]


class TestInvestigationTools(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data_dir = Path(self.temp.name)
        override = patch.object(case_store, "DATA_DIR", self.data_dir)
        override.start()
        self.addCleanup(override.stop)
        manifest = json.loads((GOLD / "fixtures/file_manifest.json").read_text(encoding="utf-8"))
        issues = json.loads((GOLD / "gold/gold_issues.json").read_text(encoding="utf-8"))
        payload = create_empty_case("案件 A 离线工具测试").model_dump(mode="json")
        payload.update(case_id=manifest["case_id"], core_issue=issues["core_issue"],
                       issue_set=issues["issue_set"], info_units=gold_lines("gold_info_units.jsonl"),
                       relations=gold_lines("gold_relations.jsonl"))
        payload["files"] = []
        for record in manifest["files"]:
            # Original uploads deliberately absent: tools must only use cached TXT.
            file = {key: record[key] for key in (
                "file_id", "filename", "file_type", "source_kind", "original_path",
                "parsed_text_path", "parse_status", "error_message",
            )}
            file.update(file_size=0, uploaded_at=payload["created_at"])
            payload["files"].append(file)
            path = self.data_dir / payload["case_id"] / file["parsed_text_path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((GOLD / record["fixture_text_path"]).read_bytes())
        self.case = CaseData.model_validate(payload)
        self.before_case = self.case.model_dump_json()
        self.before_files = self.disk_snapshot()

    def disk_snapshot(self):
        return {str(path.relative_to(self.data_dir)): path.read_bytes()
                for path in self.data_dir.rglob("*") if path.is_file()}

    def tearDown(self):
        self.assertEqual(self.case.model_dump_json(), self.before_case)
        self.assertEqual(self.disk_snapshot(), self.before_files)

    def assert_result(self, result, status, code=None):
        self.assertEqual(set(result), {"status", "items", "has_more", "error"})
        self.assertEqual(result["status"], status, result)
        self.assertEqual(result["has_more"], status == "truncated")
        if code:
            self.assertEqual(result["error"]["code"], code)
            self.assertEqual(result["items"], [])
        else:
            self.assertIsNone(result["error"])
        self.assertEqual(json.loads(json.dumps(result, ensure_ascii=False)), result)

    def test_search_default_and_hard_limit(self):
        result = search_info_units(self.case, "押金")
        self.assert_result(result, "truncated")
        self.assertEqual(len(result["items"]), 5)
        maximum = search_info_units(self.case, "押金 张某 李某", limit=8)
        self.assertEqual(len(maximum["items"]), 8)
        self.assertTrue(maximum["has_more"])

    def test_search_type_filter_and_no_matches(self):
        result = search_info_units(self.case, "押金", types=["rule"])
        self.assert_result(result, "ok")
        self.assertEqual([row["id"] for row in result["items"]], ["U008"])
        for query, types in (("不存在的关键字", None), ("押金", ["entity"])):
            with self.subTest(query=query, types=types):
                empty = search_info_units(self.case, query, types=types)
                self.assert_result(empty, "empty")
                self.assertEqual(empty["items"], [])

    def test_search_coverage_precedes_title_weight_and_ties_use_id(self):
        # U029/U036 match both terms. The exact-title hit U029 leads U036.
        result = search_info_units(self.case, "实际 损失", limit=8)
        self.assertEqual([row["id"] for row in result["items"][:2]], ["U029", "U036"])
        title_first = search_info_units(self.case, "押金", limit=8)
        self.assertEqual([row["id"] for row in title_first["items"][:2]], ["U006", "U008"])
        reordered = self.case.model_copy(deep=True)
        reordered.info_units.reverse()
        self.assertEqual(search_info_units(reordered, "押金", limit=8), title_first)

    def test_search_duplicate_keywords_do_not_change_ranking(self):
        self.assertEqual(search_info_units(self.case, "实际 损失"),
                         search_info_units(self.case, "实际 实际，损失"))

    def test_search_nfkc_and_casefold(self):
        case = self.case.model_copy(deep=True)
        case.info_units[0].content = "材料记载ＡＢＣ付款事项。"
        result = search_info_units(case, "abc")
        self.assertEqual([row["id"] for row in result["items"]], [case.info_units[0].id])

    def test_search_does_not_match_unrelated_source_quote_or_metadata(self):
        # Literal quote wording occurs only in SourceRef, not the node preview.
        for query in ("你是提前退租", self.case.case_title, "file-e256bb1ee8"):
            with self.subTest(query=query):
                self.assert_result(search_info_units(self.case, query), "empty")

    def test_previews_are_bounded_and_do_not_export_source_refs(self):
        case = self.case.model_copy(deep=True)
        case.info_units[0].content = "测试" * 110
        result = search_info_units(case, "测试")
        row = result["items"][0]
        self.assertEqual(len(row["content"]), SUMMARY_CHARS)
        self.assertTrue(row["content_truncated"])
        self.assertEqual(set(row), {"id", "type", "subtype", "title", "content",
                                    "content_truncated", "speaker"})
        self.assertNotIn("source_refs", row)
        reported = search_info_units(self.case, "入住")["items"][0]
        self.assertEqual((reported["subtype"], reported["speaker"]), ("reported", "张某"))

    def test_invalid_search_queries_and_limits(self):
        for query in (None, 12, "", "   ", "*?!", "词" * 201, ["押金"]):
            with self.subTest(query=query):
                self.assert_result(search_info_units(self.case, query), "invalid_request", "invalid_query")
        for limit in (0, -1, 9, True, 2.0, "5", None):
            with self.subTest(limit=limit):
                for tool, argument in ((search_info_units, "押金"), (get_relations, ["U029"])):
                    self.assert_result(tool(self.case, argument, limit=limit),
                                       "invalid_request", "invalid_limit")

    def test_invalid_filters(self):
        for values in ([], "event", ["unknown"], ["event", "event"], [1], [None], [["event"]]):
            with self.subTest(types=values):
                self.assert_result(search_info_units(self.case, "押金", types=values),
                                   "invalid_request", "invalid_filter")
        for values in ([], "support", ["actor_of"], ["about", "about"], [False]):
            with self.subTest(relation_types=values):
                self.assert_result(get_relations(self.case, ["U029"], relation_types=values),
                                   "invalid_request", "invalid_filter")

    def test_invalid_or_foreign_ids_reject_whole_request_before_io(self):
        for ids, code in (
            (["U029", "foreign-unit"], "unknown_info_unit"),
            (["U999"], "unknown_info_unit"),
            ([], "invalid_info_unit_ids"),
            (["U029", "U029"], "invalid_info_unit_ids"),
            (["U029"] * 6, "invalid_info_unit_ids"),
            ("U029", "invalid_info_unit_ids"),
            ([None], "invalid_info_unit_ids"),
            ([[]], "invalid_info_unit_ids"),
            ([""], "invalid_info_unit_ids"),
            ([" "], "invalid_info_unit_ids"),
        ):
            with self.subTest(ids=ids), patch("app.investigation_tools.read_parsed_document") as read:
                for tool in (get_relations, get_evidence):
                    self.assert_result(tool(self.case, ids), "invalid_request", code)
                read.assert_not_called()

    def test_id_scope_is_the_supplied_case(self):
        other = create_empty_case("其他案件")
        for tool in (get_relations, get_evidence):
            self.assert_result(tool(other, ["U029"]), "invalid_request", "unknown_info_unit")

    def test_relations_preserve_incoming_outgoing_ids_and_direction(self):
        result = get_relations(self.case, ["U036"])
        self.assert_result(result, "ok")
        rows = {row["relation"]["id"]: row for row in result["items"]}
        self.assertEqual(set(rows), {"R010", "R017"})
        self.assertEqual((rows["R010"]["relation"]["source_id"], rows["R010"]["relation"]["target_id"]),
                         ("U029", "U036"))
        self.assertEqual((rows["R017"]["relation"]["source_id"], rows["R017"]["relation"]["target_id"]),
                         ("U036", "U035"))
        self.assertEqual(rows["R010"]["relation"]["relation_type"], "contradict")
        self.assertEqual(rows["R010"]["relation"]["certainty"], "inferred")
        self.assertEqual([row["id"] for row in rows["R010"]["adjacent_info_units"]], ["U029"])

    def test_relations_never_expand_second_hop_or_derived_actor_edges(self):
        result = get_relations(self.case, ["U026"])
        self.assertEqual([row["relation"]["id"] for row in result["items"]], ["R014"])
        self.assertEqual([row["id"] for row in result["items"][0]["adjacent_info_units"]], ["U029"])
        # U029's other neighbor U036 is not returned. Actor U002 has no persisted edge.
        self.assertNotIn("U036", json.dumps(result))
        self.assert_result(get_relations(self.case, ["U002"]), "empty")

    def test_relation_filter_and_limit_and_no_duplicate_shared_edge(self):
        result = get_relations(self.case, ["U029", "U036"], relation_types=["contradict"])
        self.assert_result(result, "ok")
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["adjacent_info_units"], [])
        limited = get_relations(self.case, ["U029"], limit=1)
        self.assert_result(limited, "truncated")
        self.assertEqual(limited["items"][0]["relation"]["id"], "R010")
        self.assert_result(get_relations(self.case, ["U029"], relation_types=["support"]), "empty")

    def test_persisted_support_is_queried_without_inferring_more_edges(self):
        case = self.case.model_copy(deep=True)
        relation = Relation(id="support-test", source_id="U010", target_id="U030", relation_type="support",
                            evidence_unit_ids=["U010"], rationale="仅用于查询测试的持久化关系", certainty="inferred")
        case.relations.append(relation)
        case = CaseData.model_validate(case.model_dump(mode="json"))
        result = get_relations(case, ["U030"], relation_types=["support"])
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["relation"]["id"], relation.id)
        self.assertEqual(result["items"][0]["relation"]["source_id"], "U010")

    def test_relations_default_and_hard_limit_bound_neighbors_too(self):
        case = self.case.model_copy(deep=True)
        for index, uid in enumerate(("U010", "U012", "U022")):
            case.relations.append(Relation(
                id=f"support-{index}", source_id=uid, target_id="U030", relation_type="support",
                evidence_unit_ids=[uid], rationale="仅用于边界测试", certainty="inferred"))
        case = CaseData.model_validate(case.model_dump(mode="json"))
        ids = ["U029", "U030", "U019", "U036", "U016"]
        for limit, expected in ((5, 5), (8, 8)):
            with self.subTest(limit=limit):
                result = get_relations(case, ids, limit=limit)
                self.assert_result(result, "truncated")
                self.assertEqual(len(result["items"]), expected)
                self.assertLessEqual(sum(len(row["adjacent_info_units"]) for row in result["items"]), limit)
        self.assertEqual(get_relations(case, ids), get_relations(case, ids, limit=5))

    def test_relation_fields_and_neighbor_summaries_have_limits(self):
        case = self.case.model_copy(deep=True)
        case.relations[0].rationale = "说明" * 200
        case.relations[0].evidence_unit_ids = [unit.id for unit in case.info_units]
        for unit in case.info_units:
            unit.content = "摘要" * 110
        row = get_relations(case, ["U019"])["items"][0]
        self.assertEqual(len(row["relation"]["rationale"]), SUMMARY_CHARS)
        self.assertTrue(row["relation"]["rationale_truncated"])
        self.assertEqual(len(row["relation"]["evidence_unit_ids"]), 8)
        self.assertTrue(row["relation"]["evidence_ids_truncated"])
        for neighbor in row["adjacent_info_units"]:
            self.assertEqual(len(neighbor["content"]), SUMMARY_CHARS)
            self.assertNotIn("source_refs", neighbor)

    def test_all_gold_evidence_matches_exact_cached_slices(self):
        for unit in self.case.info_units:
            with self.subTest(unit=unit.id):
                result = get_evidence(self.case, [unit.id])
                self.assert_result(result, "ok")
                self.assertEqual(len(result["items"]), len(unit.source_refs))
                for index, (row, original) in enumerate(zip(result["items"], unit.source_refs)):
                    self.assertEqual(row, {"info_unit_id": unit.id, "source_ref_index": index,
                                           "source_ref": original.model_dump(mode="json")})
                    file = next(file for file in self.case.files if file.file_id == original.file_id)
                    text = (self.data_dir / self.case.case_id / file.parsed_text_path).read_bytes().decode("utf-8")
                    self.assertEqual(text[original.start:original.end], original.quote)

    def test_evidence_reads_only_returned_files_and_reuses_cache(self):
        with patch("app.investigation_tools.read_parsed_document", wraps=case_store.read_parsed_document) as read:
            result = get_evidence(self.case, ["U029", "U026"])
        self.assert_result(result, "ok")
        self.assertEqual(read.call_count, 1)
        self.assertEqual(read.call_args.args[1].file_id, "file-e256bb1ee8")
        self.assertEqual({row["source_ref"]["file_id"] for row in result["items"]}, {"file-e256bb1ee8"})
        quotes = "".join(row["source_ref"]["quote"] for row in result["items"])
        self.assertNotIn("后来也是跟房东重新签了合同", quotes)

    def test_evidence_missing_cache_rejects_partial_result_without_parsing(self):
        with patch("app.investigation_tools.read_parsed_document", wraps=case_store.read_parsed_document) as read:
            # U029 succeeds first; U036 fails. No partial response is exposed.
            original_reader = case_store.read_parsed_document

            def unavailable(case_id, file):
                if file.file_id == "file-06b00bee7b":
                    from app.errors import AppError
                    raise AppError("parsed_cache_unavailable", "Cannot read cached UTF-8 TXT", 409)
                return original_reader(case_id, file)

            read.side_effect = unavailable
            self.assert_result(get_evidence(self.case, ["U029", "U036"]),
                               "error", "parsed_cache_unavailable")

    def test_evidence_actual_reader_reports_missing_cache(self):
        with patch("app.case_store.artifact_path", return_value=self.data_dir / "missing.txt"):
            self.assert_result(get_evidence(self.case, ["U029"]), "error", "parsed_cache_unavailable")

    def test_evidence_mismatched_quote_is_an_error_not_a_fact(self):
        case = self.case.model_copy(deep=True)
        ref = next(unit for unit in case.info_units if unit.id == "U029").source_refs[0]
        ref.quote = "错" * len(ref.quote)
        self.assert_result(get_evidence(case, ["U029"]), "error", "source_quote_mismatch")

    def test_evidence_ref_count_budget_preserves_complete_quotes(self):
        ids = ["U019", "U008", "U004", "U029", "U026"]
        result = get_evidence(self.case, ids)
        self.assert_result(result, "truncated")
        self.assertEqual(len(result["items"]), MAX_EVIDENCE_REFS)
        self.assertEqual(result["items"][-1]["info_unit_id"], "U004")
        self.assertEqual(result["items"][-1]["source_ref_index"], 0)
        for row in result["items"]:
            ref = row["source_ref"]
            self.assertEqual(len(ref["quote"]), ref["end"] - ref["start"])

    def test_exact_ref_count_limit_is_not_falsely_marked_truncated(self):
        result = get_evidence(self.case, ["U019", "U008", "U026"])
        self.assert_result(result, "ok")
        self.assertEqual(len(result["items"]), 8)

    def synthetic_evidence_case(self, quotes):
        case = self.case.model_copy(deep=True)
        file = case.files[0]
        text = "😀\r\n" + "\n".join(quotes) + "\n未被引用的尾部文本"
        refs = []
        offset = 3
        for quote in quotes:
            refs.append(SourceRef(file_id=file.file_id, source_kind=file.source_kind,
                                  quote=quote, start=offset, end=offset + len(quote)))
            offset += len(quote) + 1
        unit = InfoUnit.model_validate(case.info_units[0].model_dump(mode="json") | {
            "id": "synthetic", "source_refs": [ref.model_dump(mode="json") for ref in refs],
        })
        case.info_units.append(unit)
        # Supply cached content in memory, without editing any fixed fixture.
        from app.schemas import ParsedDocument
        return case, ParsedDocument(file_id=file.file_id, text=text)

    def test_evidence_character_budget_never_shortens_quotes_or_offsets(self):
        case, doc = self.synthetic_evidence_case(["甲" * 2000, "乙" * 2000, "丙"])
        with patch("app.investigation_tools.read_parsed_document", return_value=doc):
            result = get_evidence(case, ["synthetic"])
        self.assert_result(result, "truncated")
        self.assertEqual(sum(len(row["source_ref"]["quote"]) for row in result["items"]), MAX_EVIDENCE_CHARS)
        for row in result["items"]:
            ref = row["source_ref"]
            self.assertEqual(doc.text[ref["start"]:ref["end"]], ref["quote"])
        self.assertEqual(result["items"][0]["source_ref"]["start"], 3)

    def test_single_oversized_reference_is_an_explicit_error(self):
        case, doc = self.synthetic_evidence_case(["甲" * (MAX_EVIDENCE_CHARS + 1)])
        with patch("app.investigation_tools.read_parsed_document", return_value=doc) as read:
            self.assert_result(get_evidence(case, ["synthetic"]), "error", "evidence_budget_exceeded")
            read.assert_not_called()

    def test_search_and_relations_do_not_read_any_files(self):
        with patch("app.investigation_tools.read_parsed_document") as read:
            search_info_units(self.case, "押金")
            get_relations(self.case, ["U029"])
            read.assert_not_called()

    def test_response_mutations_cannot_mutate_case(self):
        search_info_units(self.case, "押金")["items"][0]["content"] = "外部修改"
        get_relations(self.case, ["U029"])["items"][0]["relation"]["evidence_unit_ids"].clear()
        get_evidence(self.case, ["U029"])["items"][0]["source_ref"]["quote"] = "外部修改"

    def test_local_investigation_can_choose_next_step_without_whole_case(self):
        hits = search_info_units(self.case, "实际 损失", types=["position", "state"])
        self.assertEqual(hits["items"][0]["id"], "U029")
        edges = get_relations(self.case, ["U029"], relation_types=["contradict"])
        neighbor = edges["items"][0]["adjacent_info_units"][0]
        self.assertEqual(neighbor["id"], "U036")
        evidence = get_evidence(self.case, ["U029", neighbor["id"]])
        self.assertEqual(len(evidence["items"]), 3)
        self.assertEqual({row["source_ref"]["file_id"] for row in evidence["items"]},
                         {"file-e256bb1ee8", "file-06b00bee7b"})
        self.assertLess(len(hits["items"]), len(self.case.info_units))
        self.assertLess(len(edges["items"]), len(self.case.relations))
        for result in (hits, edges, evidence):
            self.assertNotIn("files", result)
            self.assertNotIn("case", result)
            self.assertNotIn("workspace_state", result)


if __name__ == "__main__":
    unittest.main()
