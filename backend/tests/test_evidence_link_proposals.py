"""Same-run Link -> Hypothesis dependency, strict adoption and Issue isolation."""
import json
import unittest

import test_reasoning_proposals as fixture
from app.case_store import case_dir
from app.issue_workspaces import get_issue_workspaces
from app.reasoning_models import ReasoningProposal
from pydantic import ValidationError


class TestEvidenceLinkProposals(unittest.TestCase):
    setUp = fixture.TestReasoningProposals.setUp
    tearDown = fixture.TestReasoningProposals.tearDown
    edit = fixture.TestReasoningProposals.edit
    hypothesis = fixture.TestReasoningProposals.hypothesis
    investigate = fixture.TestReasoningProposals.investigate
    accept = fixture.TestReasoningProposals.accept

    def dependent(self, target=0, meaning="support", uid="U029"):
        return {**fixture.suggestion("evidence_link"), "meaning": meaning, "hypothesis_proposal_indices": [target],
                "info_unit_ids": [uid], "evidence_refs": [{"info_unit_id": uid, "source_ref_index": 0}]}

    def state(self):
        return get_issue_workspaces(self.case).workspaces[self.issue].reasoning

    def test_two_hypotheses_three_meanings_and_gap_are_independently_adoptable(self):
        first, second = fixture.suggestion("hypothesis"), fixture.suggestion("hypothesis")
        first["content"], second["content"] = "可能发生短暂空置且少收租金", "入住间隔可能没有导致少收租金"
        gap = {**fixture.suggestion("evidence_gap"), "hypothesis_proposal_indices": [0, 1]}
        response, _ = self.investigate([first, second, self.dependent(0, "oppose"),
            self.dependent(1, "support"), self.dependent(0, "verify", "U036"), gap], read_ids=["U029", "U036"])
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(len(result["proposals"]), 6)
        self.assertEqual(self.state().links, [])
        run = result["run_id"]
        blocked = self.accept(run, 2)
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(blocked.json()["detail"]["code"], "proposal_dependency_not_accepted")
        a = self.accept(run, 0).json()["object_id"]
        self.assertEqual(self.state().links, [])  # No cascading approval.
        self.assertEqual(self.accept(run, 2).status_code, 200)
        self.assertEqual(self.accept(run, 4).status_code, 200)  # Earlier Link must not invalidate a sibling.
        self.assertEqual(self.accept(run, 5).status_code, 409)  # Second parent still pending.
        b = self.accept(run, 1).json()["object_id"]
        self.assertEqual(self.accept(run, 5).status_code, 200)  # Gap and Link siblings share revision accounting.
        accepted = self.accept(run, 3)
        self.assertEqual(accepted.status_code, 200, accepted.text)
        again = self.accept(run, 3)
        self.assertTrue(again.json()["already_accepted"])
        self.assertEqual(again.json()["object_id"], accepted.json()["object_id"])
        self.assertEqual({(l.hypothesis_id, l.info_unit_id, l.meaning) for l in self.state().links},
                         {(a, "U029", "oppose"), (b, "U029", "support"), (a, "U036", "verify")})
        self.assertFalse(get_issue_workspaces(self.case).workspaces[self.case.core_issue].reasoning.links)

    def test_tool_rejects_foreign_wrong_type_unknown_evidence_and_dual_targets(self):
        first, _ = self.investigate([fixture.suggestion("hypothesis")])
        foreign = first.json()["proposals"][0]["proposal_id"]
        formal = self.hypothesis()
        for bad in ({**self.dependent(), "hypothesis_proposal_ids": [foreign]},
                    self.dependent(2), self.dependent(uid="foreign"),
                    {**self.dependent(), "hypothesis_ids": [formal]}):
            with self.subTest(bad=bad):
                response, _ = self.investigate([fixture.suggestion("hypothesis"), bad])
                self.assertEqual(len(response.json()["proposals"]), 1)
                run = response.json()["run_id"]
                audit = json.loads((case_dir(self.case.case_id) / f"runs/{run}.output.json").read_text(encoding="utf-8"))
                rejected = next(call for call in audit["tool_calls"] if call["name"] == "propose_evidence_link")
                self.assertNotIn("phase", rejected)
                self.assertEqual(rejected["result"]["status"], "invalid_request")
        # Formal object cannot be taken from another Issue or fabricated.
        response, _ = self.investigate([fixture.suggestion("evidence_link", "foreign-hypothesis")])
        self.assertEqual(response.json()["proposals"], [])

    def test_cross_run_and_copied_envelopes_cannot_satisfy_link_dependency(self):
        first, _ = self.investigate([fixture.suggestion("hypothesis"), self.dependent()])
        second, _ = self.investigate([fixture.suggestion("hypothesis"), self.dependent()])
        a, b = first.json()["run_id"], second.json()["run_id"]
        self.assertEqual(self.accept(b).status_code, 200)
        self.assertEqual(self.accept(a, 1).status_code, 409)
        self.assertEqual(self.accept(a).status_code, 200)
        path = case_dir(self.case.case_id) / f"runs/{a}.output.json"
        original = path.read_bytes()
        for change in ("foreign_parent", "wrong_type", "envelope"):
            audit = json.loads(original)
            if change == "envelope":
                audit["result"]["run_id"] = b
            else:
                target = second.json()["proposals"][0]["proposal_id"] if change == "foreign_parent" else first.json()["proposals"][1]["proposal_id"]
                audit["result"]["proposals"][1]["hypothesis_proposal_ids"] = [target]
            path.write_text(json.dumps(audit), encoding="utf-8")  # Temporary mock audits only.
            self.assertEqual(self.accept(a, 1).status_code, 409)
        path.write_bytes(original)
        self.assertEqual(self.accept(a, 1).status_code, 200)

    def test_user_edits_and_stale_child_or_parent_evidence_block_links(self):
        for change in ("hypothesis", "sibling_link", "child_evidence", "parent_evidence"):
            with self.subTest(change=change):
                response, _ = self.investigate([fixture.suggestion("hypothesis"),
                    self.dependent(uid="U036"), self.dependent(meaning="oppose")], read_ids=["U029", "U036"])
                run = response.json()["run_id"]
                h = self.accept(run).json()["object_id"]
                unit = next(u for u in self.case.info_units if u.id == ("U036" if change == "child_evidence" else "U029"))
                old = unit.content
                if change == "hypothesis":
                    self.edit(hypothesis={"id": h, "content": "用户修正", "judgment": "unverified"})
                elif change == "sibling_link":
                    link_id = self.accept(run, 2).json()["object_id"]
                    self.edit(link={"id": link_id, "hypothesis_id": h, "info_unit_id": "U029", "meaning": "verify", "rationale": "用户修正关系含义"})
                else:
                    unit.content = "材料已更新，须重新核对"
                rejected = self.accept(run, 1)
                self.assertEqual(rejected.status_code, 409, rejected.text)
                self.assertEqual(rejected.json()["detail"]["code"], "stale_reasoning_proposal")
                unit.content = old

    def test_legacy_formal_link_shape_and_new_single_target_constraints(self):
        formal = fixture.suggestion("evidence_link", "hyp-existing")
        self.assertEqual(ReasoningProposal.model_validate(formal).hypothesis_ids, ["hyp-existing"])
        for changes in ({"hypothesis_proposal_ids": ["proposal-" + "a" * 32]},
                        {"hypothesis_ids": [], "hypothesis_proposal_ids": []},
                        {"hypothesis_ids": [], "hypothesis_proposal_ids": ["proposal-" + "a" * 32, "proposal-" + "b" * 32]},
                        {"hypothesis_ids": [], "hypothesis_proposal_indices": [0]}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                ReasoningProposal.model_validate({**formal, **changes})


if __name__ == "__main__":
    unittest.main()
