"""Tool-language and v18 guidance contracts; no claim about real-model adherence."""
import hashlib
import json
import unittest

from support import REPO
from app import investigation_agent as agent
from app.investigation_actions import PROPOSAL_TOOLS, finish_investigation
from app.prompt_loader import load_prompt


class TestInvestigationToolGuidance(unittest.TestCase):
    def test_all_exposed_descriptions_and_schema_descriptions_are_english(self):
        descriptions = []
        def visit(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key == "description":
                        descriptions.append(item)
                    else:
                        visit(item)
            elif isinstance(value, list):
                for item in value:
                    visit(item)
        definitions = agent.tool_definitions()
        visit(definitions)
        self.assertGreaterEqual(len(descriptions), 10)
        for description in descriptions:
            self.assertRegex(description, r"[A-Za-z]")
            self.assertNotRegex(description, r"[\u3400-\u9fff]")
        by_name = {d["function"]["name"]: d["function"] for d in definitions}
        for name, function in {**PROPOSAL_TOOLS, "finish_investigation": finish_investigation}.items():
            self.assertEqual(by_name[name]["description"], function.__doc__)

    def test_tool_contract_is_unchanged_except_for_descriptions(self):
        def strip(value):
            if isinstance(value, dict):
                return {k: strip(v) for k, v in value.items() if k != "description"}
            if isinstance(value, list):
                return [strip(v) for v in value]
            return value
        # Frozen native-tool contract at 75a63bb: names, parameter schemas, enums and strictness.
        digest = hashlib.sha256(json.dumps(strip(agent.tool_definitions()), sort_keys=True).encode()).hexdigest()
        self.assertEqual(digest, "17375fb930fb2cdcd9dd8163ab69830c476731dc4ab40ca4c2507d9158457ab9")

    def test_read_descriptions_retain_discovery_bounds_and_source_permissions(self):
        descriptions = {d["function"]["name"]: d["function"]["description"] for d in agent.tool_definitions()}
        for name, concepts in {
            "search_info_units": ("OR", "directory titles", "visible", "omitted", "has_more", "120", "maximum 8"),
            "get_relations": ("one-hop", "directed", "adjacent", "maximum 8"),
            "get_evidence": ("one info_unit_id", "not every InfoUnit", "offset", "next_offset", "total_count", "source_ref_index", "8000", "cached TXT"),
            "search_evidence_gaps": ("omitted", "OR", "addressing", "offset", "maximum 3", "do not grant")}.items():
            for concept in concepts:
                self.assertIn(concept, descriptions[name])

    def test_prompt_searches_existing_leads_before_missing_material_claims(self):
        prompt = load_prompt(REPO / "backend/prompts/investigation_common_prompt.md", agent.PROMPT_NAME).content
        for instruction in ("优先以该标题调用 search_info_units", "目录标题仅是发现线索", "本轮未读取",
                            "案件未提供", "部分回应", "已读取但证明力有限", "核查相关材料后仍存在的事实缺口",
                            "不要求用户重新上传已有文件", "不要求遍历所有目录项", "不设置固定调用顺序或读取数量"):
            self.assertIn(instruction, prompt)

    def test_prompt_preserves_autonomy_noops_and_goal_completion_semantics(self):
        prompt = load_prompt(REPO / "backend/prompts/investigation_common_prompt.md", agent.PROMPT_NAME).content
        for instruction in ("相对于该 Gap 已关联回应材料", "no_change=true", "不只改写理由重交",
                            "不要求固定数量的 Proposal", "不强制得到 verified、refuted 或 resolved",
                            "不能为了生成 Proposal 而修改已成立的判断", "open_questions 可保留不影响本轮完成判断"):
            self.assertIn(instruction, prompt)
