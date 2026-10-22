import json
from unittest.mock import AsyncMock, patch

from support import IsolatedCaseTest, quote_id_for
from app import case_store, llm_client


CONFIG = llm_client.LLMConfig(
    "azure", "https://test.services.ai.azure.com/openai/v1", "configured-model", "test-secret")
ISSUE = "款项是否已支付"


def proposal(*, action="create", target_info_unit_id=None, evidence_ids, **changes):
    proposed = {
        "type": "event",
        "subtype": "reported",
        "title": "甲方称已经付款",
        "content": "甲方称已经支付100元，但该说法尚未核实。",
        "evidence_ids": evidence_ids,
        "speaker": "甲",
        "side": "付款方",
        "actor": None,
        "target": None,
        "related_issue": ISSUE,
    }
    proposed.update(changes)
    return json.dumps({
        "action": action,
        "target_info_unit_id": target_info_unit_id,
        "proposed": proposed,
        "message": None,
    }, ensure_ascii=False)


def nonmutation(action, message):
    return json.dumps({
        "action": action,
        "target_info_unit_id": None,
        "proposed": None,
        "message": message,
    }, ensure_ascii=False)


class TestInfoUnitAgent(IsolatedCaseTest):
    def setUp(self):
        super().setUp()
        uploaded = self.upload(
            raw="2026-03-01\n甲：我已支付100元。\n乙：尚未收到100元。\n双方：确认租金金额为3000元。".encode("utf-8"),
            source_kind="communication",
        )
        self.case_id = uploaded["case_id"]
        self.file_id = uploaded["files"][0]["file_id"]
        case = case_store.load_case(self.case_id)
        case.core_issue = ISSUE
        case.issue_set = [ISSUE]
        case_store.save_case(case)
        self.q_paid = quote_id_for(self.case_id, self.file_id, "甲：我已支付100元。")
        self.q_unpaid = quote_id_for(self.case_id, self.file_id, "乙：尚未收到100元。")
        self.q_amount = quote_id_for(self.case_id, self.file_id, "双方：确认租金金额为3000元。")

    def model_reply(self, content):
        return llm_client.LLMReply(
            content, {"choices": [{"message": {"content": content}, "finish_reason": "stop"}]})

    def generate(self, content, payload=None):
        request = payload or {"instruction": "找到租金约定并生成一张卡片", "selected_info_unit_ids": []}
        with patch.object(llm_client, "load_config", return_value=CONFIG), patch.object(
                llm_client, "call_structured",
                new=AsyncMock(return_value=self.model_reply(content))) as call:
            response = self.client.post(
                f"/api/cases/{self.case_id}/info-unit-agent/proposals", json=request)
        return response, call

    def create_entity(self):
        response = self.client.post(f"/api/cases/{self.case_id}/info-units", json={
            "type": "entity", "subtype": "person", "title": "付款人甲",
            "content": "聊天消息中的付款方为甲。", "evidence_ids": [self.q_paid],
        })
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["case"]["info_units"][-1]

    def test_create_is_read_only_then_confirm_uses_real_source_and_context(self):
        entity = self.create_entity()
        path = self.data_dir / self.case_id / "case.json"
        before = path.read_bytes()
        response, call = self.generate(proposal(
            evidence_ids=[self.q_amount], actor=entity["id"], title="双方约定租金金额",
            content="双方确认租金金额为3000元。", speaker="双方"))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(call.await_count, 1)
        model_input = call.call_args.args[2]
        self.assertEqual(model_input["case"]["info_units"][0]["id"], entity["id"])
        paid = next(item for item in model_input["case"]["quote_units"]
                    if item["quote_id"] == self.q_paid)
        self.assertEqual(paid["role"], "message")
        self.assertEqual(len(paid["context_quote_ids"]), 1)
        agent_audits = list((self.data_dir / self.case_id / "runs").glob("agent-run-*.output.json"))
        self.assertEqual(len(agent_audits), 1)
        agent_audit = json.loads(agent_audits[0].read_text(encoding="utf-8"))
        self.assertEqual(agent_audit["status"], "success")
        self.assertEqual(agent_audit["model_calls"][0]["prompt_name"], "info_unit_agent")
        self.assertRegex(agent_audit["model_calls"][0]["prompt_version"], r"^[0-9a-f]{64}$")
        self.assertEqual(agent_audit["model_calls"][0]["schema_version"], "0.3")
        self.assertEqual(agent_audit["model_calls"][0]["model"], "configured-model")

        preview = response.json()
        self.assertEqual(preview["evidence"][0]["text"], "双方：确认租金金额为3000元。")
        self.assertIn("content", [item["field"] for item in preview["changes"]])
        confirmed = self.client.post(
            f"/api/cases/{self.case_id}/info-unit-agent/proposals/confirm",
            json={key: preview[key] for key in (
                "action", "target_info_unit_id", "proposed", "base_case_hash")},
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        saved = confirmed.json()["case"]["info_units"][-1]
        self.assertTrue(saved["id"].startswith("info-"))
        self.assertEqual(saved["source_refs"][0]["quote"], "双方：确认租金金额为3000元。")
        refreshed = self.client.get(f"/api/cases/{self.case_id}").json()["case"]
        self.assertEqual(refreshed["info_units"][-1], saved)

    def test_update_preview_and_confirm_preserve_id_untouched_fields_and_other_nodes(self):
        entity = self.create_entity()
        created = self.client.post(f"/api/cases/{self.case_id}/info-units", json={
            "type": "event", "subtype": "reported", "title": "甲称已经付款",
            "content": "甲称已经支付100元，尚未核实。", "evidence_ids": [self.q_paid],
            "speaker": "甲", "side": "付款方", "actor": entity["id"],
            "target": None, "related_issue": ISSUE,
        }).json()["case"]
        target = created["info_units"][-1]
        before_entity = created["info_units"][0]
        response, _ = self.generate(
            proposal(
                action="update", target_info_unit_id=target["id"], evidence_ids=[self.q_amount],
                title=target["title"], content="双方确认租金金额为3000元。",
                speaker=target["speaker"], side=target["side"], actor=target["actor"],
                target=target["target"], related_issue=target["related_issue"],
            ),
            {"instruction": "将金额改成 3000 元", "selected_info_unit_ids": [target["id"]]},
        )
        self.assertEqual(response.status_code, 200, response.text)
        preview = response.json()
        self.assertEqual({item["field"] for item in preview["changes"]}, {"content", "evidence_ids"})
        self.assertIn("title", preview["unchanged_fields"])
        confirmed = self.client.post(
            f"/api/cases/{self.case_id}/info-unit-agent/proposals/confirm",
            json={key: preview[key] for key in (
                "action", "target_info_unit_id", "proposed", "base_case_hash")},
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        units = confirmed.json()["case"]["info_units"]
        self.assertEqual(units[0], before_entity)
        self.assertEqual(units[1]["id"], target["id"])
        self.assertEqual(units[1]["created_at"], target["created_at"])
        self.assertEqual(units[1]["title"], target["title"])
        self.assertEqual([ref["quote"] for ref in units[1]["source_refs"]], [
            "双方：确认租金金额为3000元。",
        ])

    def test_invalid_model_operations_and_foreign_evidence_never_modify_case(self):
        entity = self.create_entity()
        path = self.data_dir / self.case_id / "case.json"
        foreign = self.upload(
            filename="foreign.txt", raw="案外材料内容。".encode("utf-8"), source_kind="document")
        foreign_quote = quote_id_for(
            foreign["case_id"], foreign["files"][0]["file_id"], "案外材料内容。")
        invalid = [
            (proposal(evidence_ids=["quote-not-current"]),
             None, "invalid_evidence_id"),
            (proposal(evidence_ids=[foreign_quote]),
             None, "invalid_evidence_id"),
            (proposal(evidence_ids=[self.q_paid], type="state", subtype="person"),
             None, "invalid_info_unit"),
            (json.dumps({
                "action": "create", "target_info_unit_id": None,
                "proposed": {**json.loads(proposal(evidence_ids=[self.q_paid]))["proposed"], "unknown": "x"},
                "message": None,
            }, ensure_ascii=False), None, "info_unit_agent_schema_invalid"),
            (proposal(
                action="update", target_info_unit_id=entity["id"], evidence_ids=[self.q_paid],
                type=entity["type"], subtype=entity["subtype"], title=entity["title"],
                content=entity["content"], speaker=entity["speaker"], side=entity["side"],
                actor=entity["actor"], target=entity["target"],
                related_issue=entity["related_issue"],
             ), {"instruction": "不要改动", "selected_info_unit_ids": [entity["id"]]},
             "info_unit_agent_no_changes"),
        ]
        for content, request, expected_code in invalid:
            with self.subTest(expected_code=expected_code):
                before = path.read_bytes()
                response, _ = self.generate(content, request)
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(response.json()["detail"]["code"], expected_code)
                self.assertEqual(path.read_bytes(), before)

        before_missing = path.read_bytes()
        missing, _ = self.generate(
            proposal(action="update", target_info_unit_id="info-foreign", evidence_ids=[self.q_paid]),
            {"instruction": "修改不存在的节点", "selected_info_unit_ids": []},
        )
        self.assertEqual(missing.status_code, 404, missing.text)
        self.assertEqual(path.read_bytes(), before_missing)

        invalid_context = self.client.post(
            f"/api/cases/{self.case_id}/info-unit-agent/proposals",
            json={"instruction": "修改选中节点", "selected_info_unit_ids": ["info-foreign"]},
        )
        self.assertEqual(invalid_context.status_code, 422, invalid_context.text)
        self.assertEqual(invalid_context.json()["detail"]["code"], "invalid_info_unit_agent_context")
        self.assertEqual(path.read_bytes(), before_missing)

    def test_selection_is_context_while_clarify_and_unsupported_never_write(self):
        entity = self.create_entity()
        path = self.data_dir / self.case_id / "case.json"

        selected_create, call = self.generate(
            proposal(evidence_ids=[self.q_unpaid], title="乙方未收到款项",
                     content="乙方称尚未收到100元，仍需进一步核实。", speaker="乙"),
            {"instruction": "再新增一张记录付款行为的卡片", "selected_info_unit_ids": [entity["id"]]},
        )
        self.assertEqual(selected_create.status_code, 200, selected_create.text)
        self.assertEqual(selected_create.json()["action"], "create")
        self.assertEqual(call.await_count, 1)
        self.assertEqual(
            call.call_args.args[2]["request"]["selected_info_unit_ids"], [entity["id"]])
        self.assertNotIn("action", call.call_args.args[2]["request"])
        for title, content, evidence_id, speaker in (
            ("押金付款记录", "甲称押金已经支付100元，尚未核实。", self.q_paid, "甲"),
            ("押金收款争议", "乙称押金尚未收到100元，尚待核实。", self.q_unpaid, "乙"),
        ):
            created = self.client.post(f"/api/cases/{self.case_id}/info-units", json={
                "type": "event", "subtype": "reported", "title": title,
                "content": content, "evidence_ids": [evidence_id], "speaker": speaker,
                "side": None, "actor": None, "target": None, "related_issue": ISSUE,
            })
            self.assertEqual(created.status_code, 201, created.text)
        before = path.read_bytes()

        clarify, clarify_call = self.generate(
            nonmutation("clarify", "存在多个与押金有关的节点，请说明要修改哪一张卡。"),
            {"instruction": "修改押金那张卡", "selected_info_unit_ids": []},
        )
        self.assertEqual(clarify.status_code, 200, clarify.text)
        self.assertEqual(clarify.json()["action"], "clarify")
        self.assertIsNone(clarify.json()["proposed"])
        self.assertEqual(clarify.json()["changes"], [])
        self.assertEqual(clarify_call.await_count, 1)
        self.assertEqual(path.read_bytes(), before)

        unsupported, unsupported_call = self.generate(
            nonmutation("unsupported", "当前 Agent 不支持删除 InfoUnit。"),
            {"instruction": "删除这张卡", "selected_info_unit_ids": [entity["id"]]},
        )
        self.assertEqual(unsupported.status_code, 200, unsupported.text)
        self.assertEqual(unsupported.json()["action"], "unsupported")
        self.assertEqual(unsupported_call.await_count, 1)
        self.assertEqual(path.read_bytes(), before)

    def test_cancel_and_stale_confirmation_do_not_write(self):
        path = self.data_dir / self.case_id / "case.json"
        before = path.read_bytes()
        response, _ = self.generate(proposal(evidence_ids=[self.q_paid]))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(path.read_bytes(), before)  # Cancellation is simply no confirm request.
        preview = response.json()

        self.create_entity()
        changed = path.read_bytes()
        stale = self.client.post(
            f"/api/cases/{self.case_id}/info-unit-agent/proposals/confirm",
            json={key: preview[key] for key in (
                "action", "target_info_unit_id", "proposed", "base_case_hash")},
        )
        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertEqual(stale.json()["detail"]["code"], "stale_info_unit_proposal")
        self.assertEqual(path.read_bytes(), changed)


if __name__ == "__main__":
    import unittest
    unittest.main()
