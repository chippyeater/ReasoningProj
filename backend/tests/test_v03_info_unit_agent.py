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
    }, ensure_ascii=False)


class TestInfoUnitAgent(IsolatedCaseTest):
    def setUp(self):
        super().setUp()
        uploaded = self.upload(
            raw="2026-03-01\n甲：我已支付100元。\n乙：尚未收到100元。".encode("utf-8"),
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

    def model_reply(self, content):
        return llm_client.LLMReply(
            content, {"choices": [{"message": {"content": content}, "finish_reason": "stop"}]})

    def generate(self, content, payload=None):
        request = payload or {"action": "create", "instruction": "新增甲方付款陈述"}
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
            evidence_ids=[self.q_paid], actor=entity["id"]))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(call.await_count, 1)
        model_input = call.call_args.args[2]
        self.assertEqual(model_input["case"]["info_units"][0]["id"], entity["id"])
        paid = next(item for item in model_input["case"]["quote_units"]
                    if item["quote_id"] == self.q_paid)
        self.assertEqual(paid["role"], "message")
        self.assertEqual(len(paid["context_quote_ids"]), 1)

        preview = response.json()
        self.assertEqual(preview["evidence"][0]["text"], "甲：我已支付100元。")
        self.assertIn("content", [item["field"] for item in preview["changes"]])
        confirmed = self.client.post(
            f"/api/cases/{self.case_id}/info-unit-agent/proposals/confirm",
            json={key: preview[key] for key in (
                "action", "target_info_unit_id", "proposed", "base_case_hash")},
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        saved = confirmed.json()["case"]["info_units"][-1]
        self.assertTrue(saved["id"].startswith("info-"))
        self.assertEqual(saved["source_refs"][0]["quote"], "甲：我已支付100元。")
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
                action="update", target_info_unit_id=target["id"], evidence_ids=[self.q_paid, self.q_unpaid],
                title=target["title"], content="甲称已经支付100元，乙明确表示尚未收到。",
                speaker=target["speaker"], side=target["side"], actor=target["actor"],
                target=target["target"], related_issue=target["related_issue"],
            ),
            {"action": "update", "instruction": "补充乙方未收到的证据", "target_info_unit_id": target["id"]},
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
            "甲：我已支付100元。", "乙：尚未收到100元。",
        ])

    def test_invalid_model_operations_and_foreign_evidence_never_modify_case(self):
        entity = self.create_entity()
        path = self.data_dir / self.case_id / "case.json"
        foreign = self.upload(
            filename="foreign.txt", raw="案外材料内容。".encode("utf-8"), source_kind="document")
        foreign_quote = quote_id_for(
            foreign["case_id"], foreign["files"][0]["file_id"], "案外材料内容。")
        invalid = [
            (proposal(action="update", target_info_unit_id=entity["id"], evidence_ids=[self.q_paid]),
             {"action": "create", "instruction": "新增"}, "info_unit_agent_action_mismatch"),
            (proposal(evidence_ids=["quote-not-current"]),
             None, "invalid_evidence_id"),
            (proposal(evidence_ids=[foreign_quote]),
             None, "invalid_evidence_id"),
            (proposal(evidence_ids=[self.q_paid], type="state", subtype="person"),
             None, "invalid_info_unit"),
            (json.dumps({
                "action": "create", "target_info_unit_id": None,
                "proposed": {**json.loads(proposal(evidence_ids=[self.q_paid]))["proposed"], "unknown": "x"},
            }, ensure_ascii=False), None, "info_unit_agent_schema_invalid"),
            (proposal(
                action="update", target_info_unit_id=entity["id"], evidence_ids=[self.q_paid],
                type=entity["type"], subtype=entity["subtype"], title=entity["title"],
                content=entity["content"], speaker=entity["speaker"], side=entity["side"],
                actor=entity["actor"], target=entity["target"],
                related_issue=entity["related_issue"],
             ), {"action": "update", "instruction": "不要改动", "target_info_unit_id": entity["id"]},
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
        missing = self.client.post(
            f"/api/cases/{self.case_id}/info-unit-agent/proposals",
            json={"action": "update", "instruction": "修改", "target_info_unit_id": "info-foreign"},
        )
        self.assertEqual(missing.status_code, 404, missing.text)
        self.assertEqual(path.read_bytes(), before_missing)

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
