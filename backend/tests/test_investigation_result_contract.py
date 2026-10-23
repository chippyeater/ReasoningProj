"""Offline diagnosis of the 2026-10-23 proposal contract failure.

Minimized from the observed shape (two new hypotheses, two unattached gaps).
Private runtime outputs stay in their original local audit files; this fixture
uses the existing public Case A test IDs. These tests document current behavior,
not an approved change to proposal semantics.
"""
from copy import deepcopy
import json
import unittest
from typing import Annotated

from pydantic import TypeAdapter, ValidationError

import test_investigation_tools as fixtures
from app.errors import AppError
from app.investigation_agent import InvestigationResult, _strict_json, _validate_result, result_schema
from app.reasoning_models import ReasoningProposal


class TestInvestigationResultContract(unittest.TestCase):
    setUp = fixtures.TestInvestigationTools.setUp
    tearDown = fixtures.TestInvestigationTools.tearDown
    disk_snapshot = fixtures.TestInvestigationTools.disk_snapshot

    def result(self):
        pointer = {"info_unit_id": "U029", "source_ref_index": 0}
        def proposal(kind):
            return {"kind": kind, "content": "需要核实的假设或证据缺口", "hypothesis_ids": [],
                    "info_unit_ids": ["U029"], "meaning": None, "judgment": None,
                    "rationale": "当事人陈述需要独立材料核查。", "evidence_refs": [deepcopy(pointer)]}
        return {"goal": "核实实际损失", "findings": [
            {"statement": "材料中存在当事人陈述，尚未独立核实。", "info_unit_ids": ["U029"],
             "relation_ids": [], "evidence_refs": [deepcopy(pointer)]} for _ in range(3)],
            "relevant_info_unit_ids": ["U029"], "relevant_relation_ids": [], "evidence_refs": [pointer],
            "open_questions": ["实际损失仍需核查。"], "completion_status": "evidence_insufficient",
            "proposals": [proposal("hypothesis"), proposal("hypothesis"),
                          proposal("evidence_gap"), proposal("evidence_gap")]}

    def validate(self, payload, *, context=None, raw=None):
        seen = {"units": {"U029"}, "relations": set(), "evidence": {("U029", 0)}}
        return _validate_result(raw if raw is not None else json.dumps(payload, ensure_ascii=False),
                                payload["goal"], self.case, seen,
                                context if context is not None else {"reasoning": {"hypotheses": [], "gaps": []}})

    def test_two_empty_gap_targets_fail_semantics_not_json_parsing(self):
        payload = self.result()
        self.assertEqual(_strict_json(json.dumps(payload)), payload)
        with self.assertRaises(AppError) as caught:
            self.validate(payload)
        self.assertEqual(caught.exception.code, "investigation_result_invalid")
        cause = caught.exception.__cause__
        self.assertIsInstance(cause, ValidationError)
        self.assertEqual([error["loc"] for error in cause.errors()], [("proposals", 2), ("proposals", 3)])
        self.assertTrue(all(error["type"] == "value_error" for error in cause.errors()))
        self.assertTrue(all("Proposal fields do not match kind" in error["msg"] for error in cause.errors()))
        self.assertEqual(caught.exception.details["category"], "proposal_semantics")
        self.assertEqual(caught.exception.details["errors"][0]["loc"], ["proposals", 2])

    def test_legacy_runtime_field_types_still_require_kind_semantics(self):
        schema = InvestigationResult.model_json_schema()["$defs"]["ReasoningProposal"]
        self.assertNotIn("minItems", schema["properties"]["hypothesis_ids"])
        proposal = self.result()["proposals"][2]
        # All declared field types/lengths allow the actual shape. The separate
        # model_validator rejects its kind-dependent combination.
        for name, field in ReasoningProposal.model_fields.items():
            annotation = Annotated[(field.annotation, *field.metadata)] if field.metadata else field.annotation
            TypeAdapter(annotation).validate_python(proposal[name] if name in proposal else field.get_default(call_default_factory=True), strict=True)
        with self.assertRaises(ValidationError):
            ReasoningProposal.model_validate(proposal)

    def test_invalid_json_has_a_different_underlying_cause(self):
        with self.assertRaises(AppError) as caught:
            self.validate(self.result(), raw='{"goal":')
        self.assertIsInstance(caught.exception.__cause__, json.JSONDecodeError)
        self.assertNotIsInstance(caught.exception.__cause__, ValidationError)
        self.assertEqual(caught.exception.details["category"], "json")
        self.assertEqual(caught.exception.details["errors"][0]["lineno"], 1)

    def test_new_hypotheses_alone_are_valid_without_existing_targets(self):
        payload = self.result()
        payload["proposals"] = payload["proposals"][:2]
        self.assertEqual(len(self.validate(payload).proposals), 2)

    def test_existing_parent_gap_passes_but_invented_proposal_ids_do_not(self):
        payload = self.result()
        payload["proposals"] = [payload["proposals"][2]]
        payload["proposals"][0]["hypothesis_ids"] = ["hyp-existing"]
        result = self.validate(payload, context={"reasoning": {"hypotheses": [{"id": "hyp-existing"}]}})
        self.assertEqual(result.proposals[0].hypothesis_ids, ["hyp-existing"])
        payload["proposals"][0]["hypothesis_ids"] = ["proposal-0"]
        with self.assertRaises(AppError) as caught:
            self.validate(payload)
        self.assertEqual(caught.exception.code, "invalid_investigation_reference")
        self.assertEqual(caught.exception.details["category"], "reference")

    def test_same_round_new_hypotheses_and_gaps_keep_original_indices(self):
        payload = self.result()
        payload["proposals"][2]["hypothesis_proposal_indices"] = [0, 1]
        payload["proposals"][3]["hypothesis_proposal_indices"] = [1]
        validated = self.validate(payload)
        self.assertEqual([p.kind for p in validated.proposals], ["hypothesis", "hypothesis", "evidence_gap", "evidence_gap"])
        self.assertEqual(validated.proposals[2].hypothesis_proposal_indices, [0, 1])
        # Forward reference is also unambiguous; proposals are never sorted.
        payload["proposals"] = [payload["proposals"][3], payload["proposals"][1]]
        self.assertEqual(self.validate(payload).proposals[0].hypothesis_proposal_indices, [1])

    def test_index_bounds_types_duplicates_and_combined_limit_are_strict(self):
        for indices in ([-1], [6], [True], ["0"], [0, 0], [{"run_id": "another-run", "index": 0}]):
            with self.subTest(indices=indices):
                payload = self.result()
                payload["proposals"] = [payload["proposals"][0], payload["proposals"][2]]
                payload["proposals"][1]["hypothesis_proposal_indices"] = indices
                with self.assertRaises(AppError):
                    self.validate(payload)
        payload = self.result()
        gap = payload["proposals"][2]
        gap.update(hypothesis_ids=[f"hyp-{i}" for i in range(5)], hypothesis_proposal_indices=[0])
        with self.assertRaises(ValidationError):
            ReasoningProposal.model_validate(gap)

    def test_out_of_range_wrong_kind_self_reference_and_non_gap_dependency_fail(self):
        for kind, indices in (("evidence_gap", [5]), ("evidence_gap", [1]), ("hypothesis", [0])):
            payload = self.result()
            payload["proposals"] = payload["proposals"][:2]
            payload["proposals"][1].update(kind=kind, hypothesis_proposal_indices=indices)
            with self.subTest(kind=kind, indices=indices), self.assertRaises(AppError) as caught:
                self.validate(payload)
            self.assertEqual(caught.exception.details["category"], "proposal_semantics")
        # Gap -> Gap is prohibited even when the target Gap is itself valid.
        payload = self.result()
        payload["proposals"][2]["hypothesis_proposal_indices"] = [3]
        payload["proposals"][3]["hypothesis_proposal_indices"] = [0]
        with self.assertRaises(AppError) as caught:
            self.validate(payload)
        self.assertEqual(caught.exception.details["errors"][0]["loc"], ["proposals", 2, "hypothesis_proposal_indices", 0])

    def test_provider_schema_frontloads_kind_constraints_without_changing_legacy_reads(self):
        variants = result_schema()["$defs"]["ReasoningProposal"]["anyOf"]
        self.assertEqual(len(variants), 5)
        for variant in variants:
            props = variant["properties"]
            self.assertFalse(variant["additionalProperties"])
            self.assertEqual(set(variant["required"]), set(props))
            self.assertEqual(props["hypothesis_proposal_indices"]["items"]["minimum"], 0)
            self.assertEqual(props["hypothesis_proposal_indices"]["items"]["exclusiveMaximum"], 6)
            if props["kind"]["enum"] == ["evidence_gap"]:
                self.assertTrue(props["hypothesis_ids"].get("minItems") == 1 or props["hypothesis_proposal_indices"].get("minItems") == 1)
                self.assertEqual(props["content"]["type"], "string")
            else:
                self.assertEqual(props["hypothesis_proposal_indices"]["maxItems"], 0)
        old = ReasoningProposal.model_validate(self.result()["proposals"][0])
        self.assertEqual(old.hypothesis_proposal_indices, [])

    def test_unretrieved_source_ref_is_a_reference_error_not_a_structure_error(self):
        payload = self.result()
        payload["proposals"] = [payload["proposals"][0]]
        payload["proposals"][0]["evidence_refs"][0]["source_ref_index"] = 99
        InvestigationResult.model_validate(payload)  # Structurally and semantically valid kind.
        with self.assertRaises(AppError) as caught:
            self.validate(payload)
        self.assertEqual(caught.exception.code, "invalid_investigation_reference")
