"""New/merged extraction evidence -> native Gap update -> explicit, revision-safe adoption."""
import json
import httpx
from unittest.mock import AsyncMock, patch

from support import IsolatedCaseTest, model_result, model_unit, quote_id_for
import test_extraction_api as extraction_fixture
import test_investigation_agent as replies
from app import case_store, investigation_tools, llm_client
from app.issue_workspaces import get_issue_workspaces, investigation_context
from app.schemas import RunStatus, StageRun


class TestGapUpdateProposals(IsolatedCaseTest):
    run_with_reply = extraction_fixture.TestExtractionAPI.run_with_reply

    def setUp(self):
        super().setUp()
        lines = [f"原材料第{i}条记载租赁争议" for i in range(9)]
        self.uploaded = self.upload(raw="\n".join(lines).encode("utf-8"), source_kind="testimony")
        self.cid = self.uploaded["case_id"]
        fid = self.uploaded["files"][0]["file_id"]
        unit = model_unit(fid, quote_id_for(self.cid, fid, lines[0]), content="当事人陈述尚有租赁履行争议。",
                          related_issue=None, evidence_ids=[quote_id_for(self.cid, fid, line) for line in lines])
        response, _ = self.run_with_reply(self.uploaded, model_result([unit]))
        self.assertEqual(response.status_code, 200, response.text)
        self.case = case_store.load_case(self.cid)
        self.issue, self.old_id = self.case.core_issue, self.case.info_units[0].id
        self.hids = [self.edit(hypothesis={"content": content}).json()["object_id"] for content in (
            "新租约实际起租，原出租人没有空置损失", "新租约尚未实际履行，仍有空置损失")]
        self.gid = self.edit(gap={"content": "新租约的起租约定是否已实际履行？", "hypothesis_ids": self.hids,
                                 "basis_info_unit_ids": [self.old_id]}).json()["object_id"]
        self.original_gap = self.state().gaps[0].model_copy(deep=True)

    def state(self):
        return get_issue_workspaces(case_store.load_case(self.cid)).workspaces[self.issue].reasoning

    def edit(self, **values):
        response = self.client.patch(f"/api/cases/{self.cid}/issue-reasoning", json={"issue": self.issue, **values})
        self.assertEqual(response.status_code, 200, response.text)
        return response

    def supplement(self, *, merge=False, count=1):
        response = self.client.post(f"/api/cases/{self.cid}/files", data={
            "source_kinds": '{"new-contract.txt":"document"}'},
            files={"files": ("new-contract.txt", "新合同约定起租，未载明实际收款。".encode("utf-8"))})
        self.assertEqual(response.status_code, 200, response.text)
        self.fid = response.json()["case"]["files"][-1]["file_id"]
        quote = quote_id_for(self.cid, self.fid, "新合同约定起租，未载明实际收款。")
        if merge:
            units = [model_unit(self.fid, quote, content=self.case.info_units[0].content, related_issue=None)]
        else:
            units = [model_unit(self.fid, quote, title=f"补充合同记载{i}", content=f"补充合同第{i}项约定起租，实际收款待核对。",
                                type="state", subtype=None, speaker="新合同", related_issue=None) for i in range(count)]
        response, _ = self.run_with_reply(self.uploaded, model_result(units))
        self.assertEqual(response.status_code, 200, response.text)
        self.case = case_store.load_case(self.cid)
        self.uid = self.old_id if merge else self.case.info_units[-1].id
        self.source_index = next(i for i, ref in enumerate(next(u for u in self.case.info_units if u.id == self.uid).source_refs)
                                 if ref.file_id == self.fid)
        self.run = self.case.stage_runs[-1]

    def investigate(self, **overrides):
        requests = []
        args = {"gap_id": self.gid, "addressing_info_unit_ids": [self.uid], "status": "open",
                "rationale": "合同仅记载起租约定，尚无实际履行凭证；部分回应，仍需实际收款或入住记录核对。",
                "evidence_refs": [{"info_unit_id": self.uid, "source_ref_index": self.source_index}], **overrides}
        async def model(config, messages, definitions, schema=None, **kwargs):
            state = json.loads(messages[1]["content"])
            requests.append(state)
            if len(requests) == 1:
                return replies.tool("search_info_units", {"query": "合同 租赁", "limit": 1}, "search")
            if len(requests) == 2:
                return replies.tool("get_evidence", {"info_unit_ids": [state["materials"]["info_units"]["items"][0]["id"]]}, "read")
            if len(requests) == 3:
                return replies.tool("propose_evidence_gap_update", args)
            return replies.finish("检查新合同对已有缺口的回应", [self.uid], refs=[(self.uid, self.source_index)],
                                  statement="新合同只载明约定，实际履行仍待核实。", status="evidence_insufficient",
                                  questions=["实际起租履行情况尚待独立核实。"])
        with patch.object(llm_client, "load_config", return_value=replies.CONFIG), patch.object(
                llm_client, "call_tools", new=AsyncMock(side_effect=model)):
            response = self.client.post(f"/api/cases/{self.cid}/agent/run", json={
                "goal": "检查新合同对已有缺口的回应", "current_issue": self.issue, "selected_reasoning_ids": [self.gid]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(requests), 4, response.text)
        return response.json(), requests

    def accept(self, result, **changes):
        return self.client.post(f"/api/cases/{self.cid}/reasoning-proposals/accept", json={
            "issue": self.issue, "run_id": result["run_id"], "proposal_id": result["proposals"][0]["proposal_id"], **changes})

    def test_new_contract_partly_addresses_original_shared_gap_and_requires_adoption(self):
        self.supplement()
        path = self.data_dir / self.cid / "issue_workspaces.json"
        before = path.read_bytes()
        result, requests = self.investigate()
        self.assertEqual(path.read_bytes(), before)  # Context/Proposal generation leaves Canvas and reasoning alone.
        self.assertEqual(self.state().gaps[0], self.original_gap)
        self.assertNotIn("recent_extraction", requests[0])
        self.assertEqual(requests[0]["materials"]["info_units"]["items"], [])
        self.assertEqual(requests[0]["materials"]["source_refs"]["items"], [])
        self.assertIn(self.uid, [u["id"] for u in requests[1]["materials"]["info_units"]["items"]])
        self.assertEqual(requests[1]["materials"]["source_refs"]["items"], [])
        self.assertTrue(any(ref["info_unit_id"] == self.uid and ref["source_ref"]["file_id"] == self.fid
                            for ref in requests[2]["materials"]["source_refs"]["items"]))
        self.assertEqual(requests[0]["reasoning"]["gaps"][0]["hypothesis_ids"], self.hids)
        self.assertEqual([p["kind"] for p in result["proposals"]], ["evidence_gap_update"])
        self.assertEqual(result["proposals"][0]["gap_id"], self.gid)
        judgments = [h.judgment for h in self.state().hypotheses]
        accepted = self.accept(result)
        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.assertEqual(accepted.json()["object_id"], self.gid)
        gap = self.state().gaps[0]
        self.assertEqual(len(self.state().gaps), 1)
        for field in ("id", "content", "basis_info_unit_ids", "hypothesis_ids"):
            self.assertEqual(getattr(gap, field), getattr(self.original_gap, field))
        self.assertEqual(gap.addressing_info_unit_ids, [self.uid])
        self.assertEqual(gap.status, "open")
        self.assertEqual(gap.revision, self.original_gap.revision + 1)
        self.assertEqual([h.judgment for h in self.state().hypotheses], judgments)
        audit = json.loads((self.data_dir / self.cid / f"runs/{result['run_id']}.output.json").read_text(encoding="utf-8"))
        self.assertEqual(audit["proposal_receipt"]["target_gap_revisions"][self.gid], self.original_gap.revision)
        after = path.read_bytes()
        again = self.accept(result)
        self.assertEqual(again.status_code, 200, again.text)
        self.assertTrue(again.json()["already_accepted"])
        self.assertEqual(path.read_bytes(), after)

    def test_merged_old_node_uses_new_source_beyond_existing_evidence_tool_window(self):
        self.supplement(merge=True)
        self.assertEqual(self.uid, self.old_id)
        self.assertEqual(self.source_index, 9)
        self.assertEqual(len(self.case.info_units), 1)
        self.assertIsNone(self.case.info_units[0].related_issue)
        result, requests = self.investigate()
        refs = requests[2]["materials"]["source_refs"]["items"]
        self.assertIn((self.uid, 9, self.fid), [(r["info_unit_id"], r["source_ref_index"], r["source_ref"]["file_id"]) for r in refs])
        self.assertLessEqual(len(refs), investigation_tools.MAX_EVIDENCE_REFS)
        # The bounded read prioritizes the new source without renumbering it.
        self.assertEqual(len(investigation_tools.get_evidence(self.case, [self.uid])["items"]), 8)
        self.assertEqual(self.accept(result).status_code, 200)
        self.assertEqual(self.state().gaps[0].addressing_info_unit_ids, [self.old_id])

    def hidden_gap_run(self, extra_count=2):
        extras = [self.edit(hypothesis={"content": f"需要核对实际交付条件{i}。"}).json()["object_id"]
                  for i in range(extra_count)]
        self.hids += extras
        question = "新租约的起租约定是否已实际履行？" + "核查实际履行与收款依据。" * 30
        self.edit(gap={"id": self.gid, "content": question, "hypothesis_ids": self.hids,
                       "basis_info_unit_ids": [self.old_id]})
        self.supplement()
        before = self.state().model_copy(deep=True)
        path = self.data_dir / self.cid / "issue_workspaces.json"
        original_bytes = path.read_bytes()
        requests = []
        pointers = [{"info_unit_id": self.uid, "source_ref_index": self.source_index}]
        def model(request):
            wire = json.loads(request.content)
            requests.append(wire)
            state = json.loads(wire["messages"][1]["content"])
            names = {d["function"]["name"] for d in wire["tools"]}
            self.assertEqual([m["role"] for m in wire["messages"]], ["system", "user"])
            self.assertIn("propose_evidence_gap_update", names)
            self.assertIn("search_evidence_gaps", names)
            self.assertIn("propose_hypothesis_status", names)
            self.assertLessEqual(len(wire["messages"][1]["content"]), 24000)
            if len(requests) == 1:
                self.assertEqual(len(state["reasoning"]["hypotheses"]), 3)
                self.assertEqual(state["reasoning"]["gaps"], [])
                self.assertTrue(state["reasoning"]["has_more"])
                reply = replies.tool("search_info_units", {"query": "补充合同", "limit": 1}, "find-material")
            elif len(requests) == 2:
                self.assertEqual(state["materials"]["info_units"]["items"][0]["id"], self.uid)
                reply = replies.tool("search_evidence_gaps", {"query": "起租", "info_unit_ids": None, "limit": 1, "offset": 0}, "find-gap")
            else:
                gap = state["reasoning"]["gaps"][0]
                self.assertEqual(gap["id"], self.gid)
                self.assertEqual(gap["content"], question)  # No 300-character initial-view truncation.
                visible = {h["id"] for h in state["reasoning"]["hypotheses"]}
                self.assertEqual(len(visible), min(5, len(self.hids)))
                self.assertTrue(visible <= set(self.hids))
                self.assertEqual(gap["hypothesis_ids"], [hid for hid in self.hids if hid in visible])
                self.assertEqual(gap["references_truncated"], len(self.hids) > 5)
                self.assertNotIn("revision", json.dumps(state["reasoning"]))
                if len(requests) == 3:
                    self.assertEqual(state["materials"]["source_refs"]["items"], [])
                    reply = replies.tool("get_evidence", {"info_unit_ids": [self.uid]}, "read-source")
                elif len(requests) == 4:
                    reply = replies.tool("propose_evidence_gap_update", {"gap_id": gap["id"],
                        "addressing_info_unit_ids": [self.uid], "status": "open", "evidence_refs": pointers,
                        "rationale": "补充合同仅记载起租约定，实际履行仍需核对。"}, "update-original")
                else:
                    reply = replies.finish("核查新增合同", [self.uid], refs=[(self.uid, self.source_index)],
                        questions=["实际履行仍待核实。"], status="evidence_insufficient")
            return httpx.Response(200, json=reply.response)
        real_client = httpx.AsyncClient
        with patch.object(llm_client, "load_config", return_value=replies.CONFIG), patch.object(
                llm_client.httpx, "AsyncClient", side_effect=lambda **kwargs:
                real_client(transport=httpx.MockTransport(model), **kwargs)):
            response = self.client.post(f"/api/cases/{self.cid}/agent/run", json={"goal": "核查新增合同",
                "current_issue": self.issue, "selected_reasoning_ids": self.hids[:3]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(path.read_bytes(), original_bytes)
        self.assertEqual(self.state(), before)
        result = response.json()
        self.assertEqual([p["kind"] for p in result["proposals"]], ["evidence_gap_update"])
        self.assertEqual(result["proposals"][0]["gap_id"], self.gid)
        audit = json.loads((self.data_dir / self.cid / f"runs/{result['run_id']}.output.json").read_text(encoding="utf-8"))
        self.assertTrue(audit["tool_calls"][1]["read_progress"])
        self.assertEqual(audit["tool_calls"][1]["no_progress"], 0)
        for call, recorded in zip(requests, audit["model_calls"]):
            self.assertEqual(call, recorded["request"])
        self.assertEqual(audit["proposal_receipt"]["target_gap_revisions"][self.gid], before.gaps[0].revision)
        self.assertTrue(set(self.hids) <= audit["proposal_receipt"]["target_revisions"].keys())
        return result, before

    def test_hidden_gap_is_discovered_and_updated_without_recreating_or_judging(self):
        result, before = self.hidden_gap_run()
        accepted = self.accept(result)
        self.assertEqual(accepted.status_code, 200, accepted.text)
        after = self.state()
        self.assertEqual(len(after.gaps), 1)
        for field in ("id", "content", "basis_info_unit_ids", "hypothesis_ids"):
            self.assertEqual(getattr(after.gaps[0], field), getattr(before.gaps[0], field))
        self.assertEqual(after.gaps[0].addressing_info_unit_ids, [self.uid])
        self.assertEqual([h.judgment for h in after.hypotheses], [h.judgment for h in before.hypotheses])

    def test_discovered_gap_still_rejects_stale_approval(self):
        result, _ = self.hidden_gap_run()
        self.edit(gap={"id": self.gid, "content": "用户修改了履行核查问题。", "hypothesis_ids": self.hids})
        response = self.accept(result)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"]["code"], "stale_reasoning_proposal")

    def test_discovery_projection_cannot_shorten_the_formal_gap_associations(self):
        result, before = self.hidden_gap_run(extra_count=4)
        response = self.accept(result)
        self.assertEqual(response.status_code, 200, response.text)
        after = self.state()
        self.assertEqual(after.gaps[0].hypothesis_ids, self.hids)
        self.assertEqual(len(after.gaps[0].hypothesis_ids), 6)
        self.assertEqual(after.gaps[0].basis_info_unit_ids, before.gaps[0].basis_info_unit_ids)
        self.assertEqual([h.judgment for h in after.hypotheses], [h.judgment for h in before.hypotheses])

    def test_visible_gap_with_omitted_hypotheses_also_keeps_complete_approval_targets(self):
        self.hids += [self.edit(hypothesis={"content": f"额外命题{i}"}).json()["object_id"] for i in range(4)]
        self.edit(gap={"id": self.gid, "content": self.original_gap.content, "hypothesis_ids": self.hids,
                       "basis_info_unit_ids": [self.old_id]})
        self.supplement()
        result, requests = self.investigate()
        self.assertEqual(len(requests[0]["reasoning"]["hypotheses"]), 3)
        self.assertTrue(requests[0]["reasoning"]["gaps"][0]["references_truncated"])
        response = self.accept(result)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.state().gaps[0].hypothesis_ids, self.hids)
        self.assertTrue(all(h.judgment == "unverified" for h in self.state().hypotheses))

    def test_gap_revision_and_material_changes_reject_stale_proposals(self):
        self.supplement()
        result, _ = self.investigate()
        self.edit(gap={"id": self.gid, "content": "用户修正后的履行核验条件", "hypothesis_ids": self.hids})
        stale = self.accept(result)
        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertEqual(stale.json()["detail"]["code"], "stale_reasoning_proposal")
        self.assertEqual(self.state().gaps[0].addressing_info_unit_ids, [])
        result, _ = self.investigate()
        current = case_store.load_case(self.cid)
        next(u for u in current.info_units if u.id == self.uid).content = "用户修正了新合同摘要内容。"
        case_store.save_case(current, set_current=False)
        stale = self.accept(result)
        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertEqual(stale.json()["detail"]["code"], "stale_reasoning_proposal")

    def test_single_proposition_partial_evidence_updates_original_gap_and_judgment_separately(self):
        self.edit(gap={"id": self.gid, "content": self.original_gap.content,
                       "hypothesis_ids": self.hids[:1], "basis_info_unit_ids": [self.old_id]})
        self.edit(delete_hypothesis_id=self.hids[1])
        self.supplement()
        before = self.state().model_copy(deep=True)
        requests = []
        pointers = [{"info_unit_id": self.uid, "source_ref_index": self.source_index}]
        async def model(config, messages, definitions, schema=None, **kwargs):
            state = json.loads(messages[1]["content"])
            requests.append(state)
            self.assertEqual(state["reasoning"]["gaps"][0]["hypothesis_ids"], self.hids[:1])
            self.assertEqual(len(state["reasoning"]["hypotheses"]), 1)
            if len(requests) == 1:
                return replies.tool("get_evidence", {"info_unit_ids": [self.uid]}, "source")
            if len(requests) == 2:
                return replies.tool("propose_evidence_gap_update", {"gap_id": self.gid,
                    "addressing_info_unit_ids": [self.uid], "status": "open", "evidence_refs": pointers,
                    "rationale": "新合同只记录起租约定，没有实际收款凭证，原事实问题仍未解决。"}, "update")
            if len(requests) == 3:
                return replies.tool("propose_hypothesis_status", {"hypothesis_id": self.hids[0],
                    "judgment": "unverified", "info_unit_ids": [self.uid], "evidence_refs": pointers,
                    "rationale": "约定起租不足以证明已履行及没有损失，保留待验证判断。"}, "status")
            return replies.finish("核对新合同", [self.uid], refs=[(self.uid, self.source_index)],
                status="evidence_insufficient", questions=["实际履行与收款尚待核实。"])
        with patch.object(llm_client, "load_config", return_value=replies.CONFIG), patch.object(
                llm_client, "call_tools", new=AsyncMock(side_effect=model)):
            response = self.client.post(f"/api/cases/{self.cid}/agent/run", json={
                "goal": "核对新合同", "current_issue": self.issue})
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual([p["kind"] for p in result["proposals"]], ["evidence_gap_update", "hypothesis_status"])
        self.assertEqual(result["proposals"][1]["hypothesis_ids"], self.hids[:1])
        self.assertEqual(self.state(), before)  # Both suggestions remain pending.
        self.assertEqual(self.accept(result).status_code, 200)
        after = self.state()
        self.assertEqual([h.id for h in after.hypotheses], self.hids[:1])
        self.assertEqual(after.hypotheses[0].judgment, "unverified")
        self.assertEqual(after.gaps[0].id, self.gid)
        self.assertEqual(after.gaps[0].hypothesis_ids, self.hids[:1])
        self.assertEqual(after.gaps[0].basis_info_unit_ids, before.gaps[0].basis_info_unit_ids)
        self.assertEqual(after.gaps[0].addressing_info_unit_ids, [self.uid])
        self.assertEqual(after.gaps[0].status, "open")

    def test_invalid_target_and_ungrounded_update_do_not_create_proposal(self):
        self.supplement()
        for values in ({"gap_id": "gap-outside-current-issue"}, {"addressing_info_unit_ids": []},
                       {"evidence_refs": []}, {"evidence_refs": [{"info_unit_id": self.uid, "source_ref_index": 99}]},
                       {"addressing_info_unit_ids": [self.uid, self.uid]}):
            with self.subTest(values=values):
                result, requests = self.investigate(**values)
                self.assertEqual(result["proposals"], [])
                self.assertIsNotNone(requests[3]["recovery"])
                self.assertEqual(self.state().gaps[0], self.original_gap)

    def test_latest_committed_batch_ignores_failed_runs_and_only_affects_search(self):
        self.supplement(count=12)
        failed = self.run.model_copy(deep=True, update={"id": "run-failed", "status": RunStatus.failed, "input_snapshot_path": None})
        # Validate through the normal model, including Enum parsing.
        self.case.stage_runs.append(StageRun.model_validate(failed.model_dump(mode="json")))
        case_store.save_case(self.case, set_current=False)
        context = investigation_context(self.case, self.issue, "核对合同", [self.gid])
        self.assertNotIn("recent_extraction", context)
        self.assertEqual(context["canvas"]["info_units"], [])
        self.assertEqual(investigation_tools.recent_extraction_files(self.case), {self.fid})
        matches = investigation_tools.search_info_units(self.case, "合同", limit=8)
        self.assertEqual(len(matches["items"]), 8)
        self.assertTrue(matches["has_more"])

    def test_same_material_noop_uses_existing_edit_semantics(self):
        self.supplement()
        result, _ = self.investigate()
        self.assertEqual(self.accept(result).status_code, 200)
        state = self.state().model_dump()
        second, _ = self.investigate()
        self.assertEqual(self.accept(second).status_code, 200)
        self.assertEqual(self.state().gaps[0].model_dump(), state["gaps"][0])
        self.assertEqual([h.model_dump() for h in self.state().hypotheses], state["hypotheses"])
