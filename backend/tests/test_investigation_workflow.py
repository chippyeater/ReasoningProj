"""Case A: on-demand discovery -> competing proposals -> incremental Gap response."""
from copy import deepcopy
import json
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import test_investigation_agent as f
from support import model_unit, quote_id_for
from app import case_store, llm_client, main
from app.issue_workspaces import get_issue_workspaces
from scripts.baseline_extraction import baseline_extraction


class TestInvestigationWorkflow(unittest.TestCase):
    setUp = f.TestInvestigationAgent.setUp

    def run_agent(self, goal, model):
        requests = []
        async def reply(config, messages, definitions, schema=None, **kwargs):
            state = json.loads(messages[1]["content"])
            requests.append(deepcopy(state))
            return model(state, len(requests))
        with patch.object(llm_client, "call_tools", new=AsyncMock(side_effect=reply)):
            result = self.client.post(f"/api/cases/{self.case.case_id}/agent/run", json={
                "goal": goal, "current_issue": self.issue})
        self.assertEqual(result.status_code, 200, result.text)
        return result.json(), requests

    def test_two_rounds_automatically_commit_and_preserve_shared_gap_identity(self):
        self.client = TestClient(main.app)
        self.issue = self.case.issue_set[1]
        for file in self.case.files:
            target = self.data_dir / self.case.case_id / file.original_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((f.fixtures.GOLD / "fixtures/raw" / file.filename).read_bytes())
        case_store.save_case(self.case)
        # Register this isolated imported fixture's existing files, using the
        # unchanged Extraction file-history contract, before uploading a new file.
        baseline_extraction(self.case.case_id, [file.file_id for file in self.case.files], apply=True)
        goal = "房东到底有没有实际租金损失？结合现有材料梳理可能情况及待核实事实"
        hypotheses = ["原承租人腾退后至替租人实际计租前存在少收租金的期间，房东可能有实际租金损失。",
                      "新旧租约计租连续且实际收款未减少，房东可能没有实际租金损失。"]
        gap_text = "核对腾退至新租约实际计租期间的收款是否连续。"
        def hypothesis(index, call_id=None):
            return f.tool("propose_hypothesis", {"content": hypotheses[index],
                "info_unit_ids": ["U029", "U036"], "rationale": "双方陈述冲突，成立条件有待核对。" + ("请复核。" if call_id else ""), "evidence_refs": []}, call_id or f"hyp-{index}")
        def gap(state, call_id="shared-gap"):
            return f.tool("propose_evidence_gap", {"content": gap_text,
                "basis_info_unit_ids": ["U029", "U036"], "rationale": "核对计租和收款记录，可共同区分两种解释。" + ("请复核。" if call_id != "shared-gap" else ""), "evidence_refs": [],
                "hypothesis_targets": {"hypothesis_ids": [], "hypothesis_proposal_ids": [p["proposal_id"]
                    for p in state["proposals"] if p["kind"] == "hypothesis"]}}, call_id)
        def first(state, turn):
            if turn == 1:
                self.assertTrue(all(section["items"] == [] for section in state["materials"].values()))
                self.assertEqual(state["case_info_directory"]["total_count"], 17)
                return f.tool("search_info_units", {"query": "损失", "limit": 8}, "search")
            if turn == 2:
                self.assertTrue({"U029", "U036"} <= {u["id"] for u in state["materials"]["info_units"]["items"]})
                self.assertEqual(state["materials"]["source_refs"]["items"], [])
                return hypothesis(0)
            if turn == 3:
                return hypothesis(1)
            if turn == 4:
                return gap(state)
            if turn == 5:
                return hypothesis(0, "duplicate-hyp")
            if turn == 6:
                self.assertEqual(state["recovery"]["code"], "duplicate_proposal")
                return gap(state, "duplicate-gap")
            self.assertEqual(state["recovery"]["code"], "duplicate_proposal")
            return f.finish(goal, ["U029", "U036"], status="evidence_insufficient", questions=["计租衔接及实际收款仍待核实。"],
                            answer="双方说法存在分歧，实际损失尚未证实；可审阅两种解释和共同核查条件。")
        result, _ = self.run_agent(goal, first)
        self.assertEqual([p["kind"] for p in result["proposals"]], ["hypothesis", "hypothesis", "evidence_gap"])
        self.assertEqual(result["workspace_commit"]["status"], "applied")
        first_state = get_issue_workspaces(self.case).workspaces[self.issue].reasoning
        hids = [h.id for h in first_state.hypotheses]
        gid = first_state.gaps[0].id
        original = get_issue_workspaces(self.case).workspaces[self.issue].reasoning.model_copy(deep=True)
        self.assertEqual(original.gaps[0].hypothesis_ids, hids)

        text = "张某补充提交出租人李某的收款记录，经核对新旧计租期间连续，房东实际收取租金未减少。"
        uploaded = self.client.post(f"/api/cases/{self.case.case_id}/files", data={
            "source_kinds": '{"rent-record.txt":"document"}'}, files={"files": ("rent-record.txt", text.encode("utf-8"))})
        self.assertEqual(uploaded.status_code, 200, uploaded.text)
        fid = uploaded.json()["case"]["files"][-1]["file_id"]
        quote = quote_id_for(self.case.case_id, fid, text)
        old = next(u for u in self.case.info_units if u.id == "U029")
        # Extraction adds the document source to an existing stable InfoUnit.
        unit = model_unit(fid, quote, type=old.type.value, subtype=old.subtype.value if old.subtype else None,
                          title=old.title, content=old.content, speaker=old.speaker, side=old.side,
                          related_issue=old.related_issue, actor_local_id=old.actor, target_local_id=old.target)
        entities = [model_unit(fid, quote, local_id=obj.id, type=obj.type.value, subtype=obj.subtype.value if obj.subtype else None,
                    title=obj.title, content=obj.content, speaker=obj.speaker, side=obj.side, related_issue=obj.related_issue)
                    for obj in self.case.info_units if obj.id in {old.actor, old.target}]
        raw = json.dumps({"core_issue": self.case.core_issue, "issue_set": self.case.issue_set, "info_units": [*entities, unit]}, ensure_ascii=False)
        with patch.object(llm_client, "call_structured", new=AsyncMock(return_value=llm_client.LLMReply(raw, {}))):
            extraction = self.client.post("/api/extraction/run", json={"case_id": self.case.case_id})
        self.assertEqual(extraction.status_code, 200, extraction.text)
        updated = case_store.load_case(self.case.case_id)
        self.assertEqual(len(updated.info_units), 17)
        ref_index = next(i for i, ref in enumerate(next(u for u in updated.info_units if u.id == "U029").source_refs) if ref.file_id == fid)
        pointers = [{"info_unit_id": "U029", "source_ref_index": ref_index}]
        def second(state, turn):
            if turn == 1:
                self.assertEqual(state["materials"]["info_units"]["items"], [])
                self.assertEqual(state["materials"]["source_refs"]["items"], [])
                self.assertEqual(state["reasoning"]["gaps"][0]["id"], gid)
                return f.tool("search_info_units", {"query": "损失", "limit": 1}, "search-new")
            if turn == 2:
                self.assertEqual(state["materials"]["info_units"]["items"][0]["id"], "U029")
                self.assertEqual(state["materials"]["source_refs"]["items"], [])
                return f.tool("get_evidence", {"info_unit_ids": ["U029"]}, "read-new")
            if turn == 3:
                self.assertTrue(any(r["source_ref_index"] == ref_index and r["source_ref"]["file_id"] == fid for r in state["materials"]["source_refs"]["items"]))
                return f.tool("propose_evidence_gap_update", {"gap_id": gid, "addressing_info_unit_ids": ["U029"],
                    "status": "resolved", "rationale": "补充记录回应了原缺口的计租和收款衔接问题。", "evidence_refs": pointers}, "gap-update")
            if turn in (4, 5):
                return f.tool("propose_hypothesis_status", {"hypothesis_id": hids[turn - 4],
                    "judgment": "refuted" if turn == 4 else "verified", "info_unit_ids": ["U029"],
                    "rationale": "新来源记载收款连续，建议由用户核对并确认判断。", "evidence_refs": pointers}, f"status-{turn}")
            return f.finish("检查新证据", ["U029"], refs=[("U029", ref_index)], status="completed",
                            statement="补充文件记载计租和收款连续。", answer="新文件回应了原核查条件；原缺口和假设判断的更新建议均待你确认。")
        response, _ = self.run_agent("检查新证据", second)
        self.assertEqual([p["kind"] for p in response["proposals"]], ["evidence_gap_update", "hypothesis_status", "hypothesis_status"])
        self.assertEqual(response["workspace_commit"]["status"], "applied")
        self.assertEqual([p["judgment"] for p in response["proposals"][1:]], ["refuted", "verified"])
        final = get_issue_workspaces(updated).workspaces[self.issue].reasoning
        self.assertEqual([h.id for h in final.hypotheses], hids)
        self.assertEqual([h.judgment for h in final.hypotheses], ["refuted", "verified"])
        self.assertEqual(len(final.gaps), 1)
        for field in ("id", "content", "basis_info_unit_ids", "hypothesis_ids"):
            self.assertEqual(getattr(final.gaps[0], field), getattr(original.gaps[0], field))
        self.assertEqual(final.gaps[0].addressing_info_unit_ids, ["U029"])
        self.assertEqual(final.gaps[0].status, "resolved")
        self.assertEqual([h.content for h in final.hypotheses], [h.content for h in original.hypotheses])
        self.assertEqual(final.links, [])  # Judgment citations are not formal EvidenceLinks.
