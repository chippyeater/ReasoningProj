import json

from support import IsolatedCaseTest, quote_id_for
from app import case_store
from app.schemas import LawMatch, Point, ReasoningView, Relation


ISSUE = "款项是否已支付"


class TestInfoUnitCrud(IsolatedCaseTest):
    def setUp(self):
        super().setUp()
        uploaded = self.upload(
            raw="甲为付款人。\n甲称已支付100元。\n乙称尚未收到100元。".encode("utf-8"),
            source_kind="testimony",
        )
        self.case_id = uploaded["case_id"]
        self.file_id = uploaded["files"][0]["file_id"]
        case = case_store.load_case(self.case_id)
        case.core_issue = ISSUE
        case.issue_set = [ISSUE]
        case_store.save_case(case)
        self.q_entity = quote_id_for(self.case_id, self.file_id, "甲为付款人。")
        self.q_paid = quote_id_for(self.case_id, self.file_id, "甲称已支付100元。")
        self.q_unpaid = quote_id_for(self.case_id, self.file_id, "乙称尚未收到100元。")

    def create(self, **changes):
        payload = {
            "type": "entity",
            "subtype": "person",
            "title": "付款人甲",
            "content": "材料记载甲为付款人。",
            "evidence_ids": [self.q_entity],
        }
        payload.update(changes)
        response = self.client.post(f"/api/cases/{self.case_id}/info-units", json=payload)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["case"]

    def test_quote_unit_list_and_incremental_create_preserve_existing_unit(self):
        response = self.client.get(f"/api/cases/{self.case_id}/quote-units")
        self.assertEqual(response.status_code, 200, response.text)
        quotes = response.json()
        self.assertEqual([item["text"] for item in quotes], [
            "甲为付款人。", "甲称已支付100元。", "乙称尚未收到100元。",
        ])
        self.assertEqual({item["file_id"] for item in quotes}, {self.file_id})

        first_case = self.create()
        first = first_case["info_units"][0]
        self.assertTrue(first["id"].startswith("info-"))
        self.assertEqual(first["source_refs"][0]["quote"], "甲为付款人。")

        second_case = self.create(
            type="event", subtype="reported", title="甲称已经付款",
            content="甲称已支付100元，尚未经独立核实。", evidence_ids=[self.q_paid],
            speaker="甲", actor=first["id"], related_issue=ISSUE,
        )
        self.assertEqual(len(second_case["info_units"]), 2)
        self.assertEqual(second_case["info_units"][0], first)
        self.assertNotEqual(second_case["info_units"][1]["id"], first["id"])

    def test_field_edit_preserves_id_created_at_sources_and_other_units(self):
        first_case = self.create()
        entity = first_case["info_units"][0]
        second_case = self.create(
            type="event", subtype="reported", title="甲称已经付款",
            content="甲称已支付100元，尚未经独立核实。", evidence_ids=[self.q_paid],
            speaker="甲", actor=entity["id"], related_issue=ISSUE,
        )
        event = second_case["info_units"][1]
        original_refs = event["source_refs"]

        response = self.client.patch(
            f"/api/cases/{self.case_id}/info-units/{event['id']}",
            json={
                "type": "position", "subtype": None, "title": "甲方主张已经付款",
                "content": "甲主张已经支付100元，但该说法仍待核实。", "speaker": "甲",
                "side": "付款方", "actor": entity["id"], "target": entity["id"],
                "related_issue": ISSUE,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        units = response.json()["case"]["info_units"]
        self.assertEqual(units[0], entity)
        edited = units[1]
        self.assertEqual(edited["id"], event["id"])
        self.assertEqual(edited["created_at"], event["created_at"])
        self.assertEqual(edited["source_refs"], original_refs)
        self.assertEqual((edited["type"], edited["subtype"]), ("position", None))
        self.assertEqual((edited["actor"], edited["target"]), (entity["id"], entity["id"]))

    def test_evidence_add_and_remove_rebuilds_only_the_edited_sources(self):
        first_case = self.create()
        entity = first_case["info_units"][0]
        second_case = self.create(
            type="event", subtype="reported", title="甲称已经付款",
            content="甲称已支付100元，乙称尚未收到。", evidence_ids=[self.q_paid],
            speaker="甲", actor=entity["id"], related_issue=ISSUE,
        )
        event = second_case["info_units"][1]

        added = self.client.patch(
            f"/api/cases/{self.case_id}/info-units/{event['id']}",
            json={"evidence_ids": [self.q_paid, self.q_unpaid]},
        )
        self.assertEqual(added.status_code, 200, added.text)
        added_units = added.json()["case"]["info_units"]
        self.assertEqual(added_units[0], entity)
        self.assertEqual([ref["quote"] for ref in added_units[1]["source_refs"]], [
            "甲称已支付100元。", "乙称尚未收到100元。",
        ])

        removed = self.client.patch(
            f"/api/cases/{self.case_id}/info-units/{event['id']}",
            json={"evidence_ids": [self.q_unpaid]},
        )
        self.assertEqual(removed.status_code, 200, removed.text)
        final_units = removed.json()["case"]["info_units"]
        self.assertEqual(final_units[0], entity)
        self.assertEqual([ref["quote"] for ref in final_units[1]["source_refs"]], [
            "乙称尚未收到100元。",
        ])

    def test_invalid_evidence_and_invalid_contract_do_not_modify_case(self):
        created = self.create()
        unit = created["info_units"][0]
        path = self.data_dir / self.case_id / "case.json"
        before = path.read_bytes()

        invalid_evidence = self.client.patch(
            f"/api/cases/{self.case_id}/info-units/{unit['id']}",
            json={"evidence_ids": ["quote-does-not-exist"]},
        )
        self.assertEqual(invalid_evidence.status_code, 422, invalid_evidence.text)
        self.assertEqual(invalid_evidence.json()["detail"]["code"], "invalid_evidence_id")
        self.assertEqual(path.read_bytes(), before)

        invalid_type = self.client.patch(
            f"/api/cases/{self.case_id}/info-units/{unit['id']}",
            json={"type": "state", "subtype": "person", "speaker": "甲"},
        )
        self.assertEqual(invalid_type.status_code, 422, invalid_type.text)
        self.assertEqual(invalid_type.json()["detail"]["code"], "invalid_info_unit")
        self.assertEqual(path.read_bytes(), before)

    def test_delete_cleans_display_state_without_touching_other_units(self):
        first_case = self.create()
        first = first_case["info_units"][0]
        second_case = self.create(
            title="收款人乙", content="材料同时记载乙方身份。", evidence_ids=[self.q_unpaid],
        )
        second = second_case["info_units"][1]
        case = case_store.load_case(self.case_id)
        state = case.workspace_state
        state.selected_node_ids = [first["id"], second["id"]]
        state.focused_node_id = first["id"]
        state.node_positions = {first["id"]: Point(x=1, y=2), second["id"]: Point(x=3, y=4)}
        state.display_levels = {first["id"]: 2, second["id"]: 3}
        case_store.save_case(case)

        response = self.client.delete(f"/api/cases/{self.case_id}/info-units/{first['id']}")
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()["case"]
        self.assertEqual(result["info_units"], [second])
        workspace = result["workspace_state"]
        self.assertEqual(workspace["selected_node_ids"], [second["id"]])
        self.assertIsNone(workspace["focused_node_id"])
        self.assertNotIn(first["id"], workspace["node_positions"])
        self.assertNotIn(first["id"], workspace["display_levels"])
        self.assertEqual(workspace["node_positions"][second["id"]], {"x": 3.0, "y": 4.0})

    def test_delete_rejects_business_references_without_cascade(self):
        first_case = self.create()
        entity = first_case["info_units"][0]
        second_case = self.create(
            type="event", subtype="reported", title="甲称已经付款",
            content="甲称已支付100元，尚未经独立核实。", evidence_ids=[self.q_paid],
            speaker="甲", actor=entity["id"], related_issue=ISSUE,
        )
        event = second_case["info_units"][1]
        case = case_store.load_case(self.case_id)
        case.relations.append(Relation(
            id="relation-1", source_id=event["id"], target_id=entity["id"],
            relation_type="about", evidence_unit_ids=[event["id"], entity["id"]],
            rationale="付款陈述涉及付款人。", certainty="explicit",
        ))
        case.reasoning_views.append(ReasoningView.model_validate({
            "id": "view-1", "question": "谁主张已经付款？", "view_type": "conflict",
            "selection_reason": "比较付款争议。", "created_at": event["created_at"],
            "payload": {
                "view_type": "conflict", "title": "付款争议", "missing_information": [],
                "sections": [{
                    "id": "section-1", "title": "付款人", "placement": "left",
                    "items": [{
                        "info_unit_id": entity["id"], "role": "context",
                        "emphasis": False, "default_expanded": False,
                    }],
                }],
            },
        }))
        case.law_matches.append(LawMatch(
            law_id="law-1", issue=ISSUE, related_info_unit_ids=[entity["id"]],
            relevance_reason="该规则涉及付款主体。",
        ))
        case_store.save_case(case)
        path = self.data_dir / self.case_id / "case.json"
        before = path.read_bytes()

        response = self.client.delete(f"/api/cases/{self.case_id}/info-units/{entity['id']}")
        self.assertEqual(response.status_code, 409, response.text)
        detail = response.json()["detail"]
        self.assertEqual(detail["code"], "info_unit_in_use")
        self.assertEqual({item["kind"] for item in detail["references"]}, {
            "info_unit", "relation", "reasoning_view", "law_match",
        })
        self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    import unittest
    unittest.main()
