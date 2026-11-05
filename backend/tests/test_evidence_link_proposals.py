"""Same-run Link -> Hypothesis dependency, strict adoption and Issue isolation."""
import json
import unittest

import test_reasoning_proposals as fixture
from app.case_store import case_dir
from app.issue_workspaces import get_issue_workspaces
from app.reasoning_models import PROPOSAL_ADAPTER
from pydantic import ValidationError


class TestEvidenceLinkProposals(unittest.TestCase):
    setUp = fixture.TestReasoningProposals.setUp
    tearDown = fixture.TestReasoningProposals.tearDown
    edit = fixture.TestReasoningProposals.edit
    hypothesis = fixture.TestReasoningProposals.hypothesis
    investigate = fixture.TestReasoningProposals.investigate

    def dependent(self, target=0, meaning="support", uid="U029"):
        return {**fixture.suggestion("evidence_link"), "meaning": meaning, "hypothesis_proposal_indices": [target],
                "info_unit_ids": [uid], "evidence_refs": [{"info_unit_id": uid, "source_ref_index": 0}]}

    def state(self):
        return get_issue_workspaces(self.case).workspaces[self.issue].reasoning









    def test_legacy_formal_link_shape_and_new_single_target_constraints(self):
        formal = fixture.suggestion("evidence_link", "hyp-existing")
        self.assertEqual(PROPOSAL_ADAPTER.validate_python(formal).hypothesis_ids, ["hyp-existing"])
        for changes in ({"hypothesis_proposal_ids": ["proposal-" + "a" * 32]},
                        {"hypothesis_ids": [], "hypothesis_proposal_ids": []},
                        {"hypothesis_ids": [], "hypothesis_proposal_ids": ["proposal-" + "a" * 32, "proposal-" + "b" * 32]},
                        {"hypothesis_ids": [], "hypothesis_proposal_indices": [0]}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                PROPOSAL_ADAPTER.validate_python({**formal, **changes})


if __name__ == "__main__":
    unittest.main()
