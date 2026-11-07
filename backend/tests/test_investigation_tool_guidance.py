"""Exact native tool descriptions and InfoUnit-based Proposal contracts, offline."""
import inspect
import json
import unittest

from support import REPO
from app import investigation_agent as agent
from app.investigation_actions import PROPOSAL_TOOLS, finish_investigation
from app.errors import AppError
from app.reasoning_models import MaterialProposal, EvidenceGapProposal, EvidenceGapUpdateProposal
from app.prompt_loader import load_prompt

EXPECTED = {'search_info_units': 'Search InfoUnits in the current case by keywords in their titles and content. Use queries based on the Goal, relevant Hypotheses, Evidence Gaps, or facts requiring investigation. Keywords use OR matching. The optional types parameter filters InfoUnit types; limit defaults to 5 (maximum 8). Returns content summaries of up to 120 characters with attribution. Results exclude InfoUnits already visible in the current material context, and has_more indicates additional matches.', 'get_relations': 'Retrieve one-hop relations involving known InfoUnits in the current case. Provide 1–5 info_unit_ids and optionally filter by relation_types. Returns directed relations and summaries of adjacent InfoUnits. limit defaults to 5 (maximum 8), and has_more indicates additional matching relations.', 'get_evidence': 'Read original SourceRef quotes associated with an InfoUnit to examine exact wording, dates, amounts, actors, and source context. Provide an info_unit_id, with offset specifying the starting position and limit specifying the number of references. Returns verified source quotes with their original indices, up to 12 references and 8,000 characters per call. Use next_offset to retrieve subsequent references when has_more is true.', 'search_evidence_gaps': 'Find existing Evidence Gaps in the current Issue. Search by query keywords matching Gap questions and associated Hypotheses, or by info_unit_ids linked to Gap basis material, addressing material, or Hypothesis evidence links. Both filters may be empty for browsing. Returns Gap IDs, questions, statuses, and associated Hypotheses. limit defaults to 3 (maximum 3), and offset supports pagination.', 'propose_hypothesis': 'Create a new, evidence-testable Hypothesis. Use content to state one clear proposition and rationale to explain its basis and uncertainty. Include relevant obtained InfoUnit IDs when available. Returns a proposal_id for referencing the new Hypothesis within the current investigation.', 'propose_evidence_link': 'Create an Evidence Link between a Hypothesis and one obtained InfoUnit. Identify the Hypothesis using its existing hypothesis_id or a proposal_id returned earlier in this investigation. Set meaning to support, oppose, or related, and use rationale to explain the relationship.', 'propose_evidence_gap': 'Create an Evidence Gap describing a specific unresolved factual question. Use content to state what needs to be established, rationale to explain its relevance, and hypothesis_targets to associate the question with one or more Hypotheses. Include relevant obtained basis_info_unit_ids when available.', 'propose_hypothesis_status': 'Update the judgment of an existing Hypothesis to unverified, verified, or refuted. Provide its hypothesis_id, the new judgment, relevant obtained info_unit_ids, and a rationale explaining the evidential basis for the change.', 'propose_evidence_gap_update': "Update an existing Evidence Gap's status, addressing materials, or both. Use status = resolved when the factual question has been sufficiently answered; otherwise use status = open and explain the remaining uncertainty. Provide addressing_info_unit_ids to associate newly obtained responding materials with the Gap. Submit an update when the status changes or new responding InfoUnits are associated.", 'finish_investigation': 'Complete the current investigation by submitting answer, answer_basis, open_questions, and completion_status. Keep answer within 1,000 characters. Use answer_basis = case_material for conclusions based on obtained case evidence, case_metadata for case metadata, or general for general knowledge. Set completion_status = completed when the Goal has been sufficiently addressed, evidence_insufficient when critical evidence is missing, budget_exhausted when the investigation budget prevents completion, or unsupported when the Goal exceeds available capabilities. Include relevant unresolved matters in open_questions.'}


class TestInvestigationToolGuidance(unittest.TestCase):
    def test_ten_descriptions_match_the_requested_text_exactly(self):
        definitions = {d["function"]["name"]: d["function"] for d in agent.tool_definitions()}
        self.assertEqual(set(definitions), set(EXPECTED))
        for name, description in EXPECTED.items():
            with self.subTest(name=name):
                self.assertEqual(definitions[name]["description"], description)
                self.assertNotRegex(description, r"[\u3400-\u9fff]")
        for name, function in {**PROPOSAL_TOOLS, "finish_investigation": finish_investigation}.items():
            self.assertEqual(function.__doc__, EXPECTED[name])

    def test_proposals_reject_source_pointers_in_schemas_and_function_arguments(self):
        definitions = {d["function"]["name"]: d["function"] for d in agent.tool_definitions()}
        for name, function in PROPOSAL_TOOLS.items():
            with self.subTest(name=name):
                schema = definitions[name]["parameters"]
                self.assertNotIn("evidence_refs", json.dumps(schema))
                self.assertNotIn("source_ref_index", json.dumps(schema))
                self.assertNotIn("evidence_refs", inspect.signature(function).parameters)
                self.assertFalse(schema["additionalProperties"])
                for field in ("evidence_refs", "source_ref_index"):
                    args = {key: None for key in schema["required"]}
                    args[field] = [] if field == "evidence_refs" else 0
                    with self.assertRaises(AppError) as caught:
                        agent._arguments(name, json.dumps(args))
                    self.assertTrue(any(e["loc"] == [field] and e["code"] == "extra_forbidden"
                                        for e in caught.exception.details["errors"]))
        for model in (MaterialProposal, EvidenceGapProposal, EvidenceGapUpdateProposal):
            self.assertNotIn("evidence_refs", json.dumps(model.model_json_schema()))

    def test_user_prompt_is_registered_and_uses_the_new_context_contract(self):
        prompt = load_prompt(REPO / "backend/prompts/investigation_common_prompt.md", agent.PROMPT_NAME)
        self.assertEqual(prompt.version, "v19")
        for field in ("case_info_directory", "core_issue", "evidence_refs"):
            self.assertNotIn(field, prompt.content)
        self.assertIn("以 InfoUnit 为基本证据单元", prompt.content)
        self.assertIn("案件中仍有其他待调查问题不影响已完成目标的交付", prompt.content)
