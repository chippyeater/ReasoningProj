import json
import unittest

from support import IsolatedCaseTest, quote_id_for
from app import case_store
from app.extraction import ExtractionFailure, materialize_extraction, parse_model_extraction


ISSUE = "款项是否已支付"


def candidate(local_id, kind, subtype, title, content, _file_id, evidence_id, **changes):
    value = {
        "local_id": local_id, "type": kind, "subtype": subtype, "title": title, "content": content,
        "evidence_ids": [evidence_id], "speaker": None, "side": None,
        "actor_local_id": None, "target_local_id": None, "related_issue": ISSUE,
    }
    value.update(changes)
    return value


def batch(*units):
    return json.dumps({"core_issue": ISSUE, "issue_set": [ISSUE], "info_units": list(units)}, ensure_ascii=False)


class TestExtractionContract(IsolatedCaseTest):
    def test_initial_extraction_rejects_verified_and_bad_local_references(self):
        entity = candidate("person", "entity", "person", "付款人甲", "材料记载甲为付款人。", "f", "甲")
        event = candidate("pay", "event", "reported", "甲称已经付款", "甲称已向对方支付款项。", "f", "甲",
                          actor_local_id="person")
        parse_model_extraction(batch(entity, event))
        invalid = [
            [entity, {**event, "subtype": "verified"}],
            [entity, {**event, "actor_local_id": "missing"}],
            [entity, {**event, "actor_local_id": "pay"}],
            [entity, {**event, "local_id": "person"}],
        ]
        for units in invalid:
            with self.subTest(units=units), self.assertRaises(ExtractionFailure):
                parse_model_extraction(batch(*units))

    def test_local_ids_map_to_formal_entity_ids(self):
        saved = self.upload(raw="甲为付款人\n甲称已付款".encode("utf-8"), source_kind="testimony")
        case = case_store.load_case(saved["case_id"])
        fid = case.files[0].file_id
        entity = candidate("person", "entity", "person", "付款人甲", "材料记载甲为付款人。", fid,
                           quote_id_for(case.case_id, fid, "甲为付款人"))
        event = candidate("pay", "event", "reported", "甲称已经付款", "甲称已向对方支付款项。", fid,
                          quote_id_for(case.case_id, fid, "甲称已付款"),
                          actor_local_id="person")
        result, _ = materialize_extraction(parse_model_extraction(batch(entity, event)), case)
        by_title = {unit.title: unit for unit in result.info_units}
        self.assertEqual(by_title["甲称已经付款"].actor, by_title["付款人甲"].id)
        self.assertNotEqual(by_title["甲称已经付款"].actor, "甲")

    def test_duplicate_entity_deduplication_remaps_dependents(self):
        saved = self.upload(raw="甲为付款人\n甲再次被记为付款人\n甲称已付款".encode("utf-8"), source_kind="testimony")
        case = case_store.load_case(saved["case_id"])
        fid = case.files[0].file_id
        a = candidate("person-a", "entity", "person", "付款人甲", "材料记载甲为付款人。", fid,
                      quote_id_for(case.case_id, fid, "甲为付款人"))
        b = candidate("person-b", "entity", "person", "付款人甲", "材料记载甲为付款人。", fid,
                      quote_id_for(case.case_id, fid, "甲再次被记为付款人"))
        event = candidate("pay", "event", "reported", "甲称已经付款", "甲称已向对方支付款项。", fid,
                          quote_id_for(case.case_id, fid, "甲称已付款"),
                          actor_local_id="person-b")
        result, merges = materialize_extraction(parse_model_extraction(batch(a, b, event)), case)
        entities = [unit for unit in result.info_units if unit.type.value == "entity"]
        self.assertEqual(len(entities), 1)
        self.assertEqual(next(unit for unit in result.info_units if unit.type.value == "event").actor, entities[0].id)
        self.assertEqual(len(merges), 1)


if __name__ == "__main__":
    unittest.main()
