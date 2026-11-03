"""Bounded Gap discovery over existing Issue objects, without source grants."""
from copy import deepcopy
import unittest

import test_investigation_agent as fixture
from app.investigation_state import KnowledgeState
from app.investigation_tools import search_evidence_gaps
from app.reasoning_models import ReasoningState


class TestGapDiscovery(unittest.TestCase):
    setUp = fixture.TestInvestigationAgent.setUp
    tearDown = fixture.TestInvestigationAgent.tearDown

    def reasoning(self):
        return ReasoningState.model_validate({
            "hypotheses": [{"id": f"h-{i}", "content": "租金连续" if i == 0 else "交接完成",
                            "updated_at": self.case.created_at} for i in range(6)],
            "gaps": [{"id": f"g-{i}", "content": f"合同履行条件{i}是否成立？", "hypothesis_ids": [f"h-{i}"],
                      "basis_info_unit_ids": ["U004"] if i == 0 else [],
                      "addressing_info_unit_ids": ["U036"] if i == 1 else [],
                      "updated_at": self.case.created_at} for i in range(6)],
            "links": [{"id": "link", "hypothesis_id": "h-2", "info_unit_id": "U029",
                       "rationale": "材料与命题有关"}]})

    def test_keywords_match_questions_and_hypotheses_and_ids_match_all_existing_associations(self):
        reasoning = self.reasoning()
        before = reasoning.model_dump_json()
        for uid, gid in (("U004", "g-0"), ("U036", "g-1"), ("U029", "g-2")):
            result = search_evidence_gaps(self.case, info_unit_ids=[uid], reasoning=reasoning)
            self.assertEqual(result["status"], "ok")
            self.assertEqual([g["id"] for g in result["items"][0]["gaps"]], [gid])
        by_hypothesis = search_evidence_gaps(self.case, "租金连续", reasoning=reasoning)
        self.assertEqual(by_hypothesis["items"][0]["gaps"][0]["id"], "g-0")
        self.assertEqual(reasoning.model_dump_json(), before)

    def test_browsing_is_bounded_and_pages_cover_every_match(self):
        reasoning = self.reasoning()
        seen = set()
        for offset in (0, 2, 4):
            result = search_evidence_gaps(self.case, limit=2, offset=offset, reasoning=reasoning)
            view = result["items"][0]
            ids = {g["id"] for g in view["gaps"]}
            self.assertEqual(len(ids), 2)
            self.assertFalse(ids & seen)
            self.assertLessEqual(len(view["hypotheses"]), 5)
            self.assertLessEqual(len(view["links"]), 5)
            self.assertEqual(result["has_more"], offset < 4)
            seen |= ids
        self.assertEqual(len(seen), 6)
        self.assertEqual(search_evidence_gaps(self.case, reasoning=reasoning, offset=6)["status"], "empty")
        self.assertEqual(search_evidence_gaps(self.case, "不存在的关键词", reasoning=reasoning)["status"], "empty")

    def test_invalid_ids_and_limits_reject_the_whole_read(self):
        for args in ({"info_unit_ids": ["U004", "foreign"]}, {"info_unit_ids": ["U004", "U004"]},
                     {"info_unit_ids": 0}, {"limit": 4}, {"limit": True}, {"query": []}, {"query": "!!!"},
                     {"offset": -1}, {"offset": True}):
            with self.subTest(args=args):
                result = search_evidence_gaps(self.case, reasoning=self.reasoning(), **args)
                self.assertEqual(result["status"], "invalid_request")
                self.assertEqual(result["items"], [])

    def test_reasoning_read_replaces_only_the_view_and_grants_no_material_or_source(self):
        initial = {"work_context": {"reasoning": {"hypotheses": [], "gaps": [], "links": [], "has_more": True}}}
        memory = KnowledgeState(initial)
        initial_copy = deepcopy(initial)
        result = search_evidence_gaps(self.case, info_unit_ids=["U004"], reasoning=self.reasoning())
        keys = memory.merge("search_evidence_gaps", {"query": None, "info_unit_ids": ["U004"], "limit": 1}, result)
        self.assertIn(("gaps", "g-0"), keys)
        self.assertFalse(memory.info_units or memory.source_refs or memory.relations)
        self.assertFalse(memory.acquisition()["has_material"])
        self.assertEqual(initial, initial_copy)
        other = search_evidence_gaps(self.case, info_unit_ids=["U036"], reasoning=self.reasoning())
        memory.merge("search_evidence_gaps", {"query": None, "info_unit_ids": ["U036"], "limit": 1}, other)
        self.assertEqual([g["id"] for g in memory.work_context["reasoning"]["gaps"]], ["g-1"])
