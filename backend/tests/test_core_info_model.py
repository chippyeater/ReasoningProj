"""Phase 2 — Core Info Model 引用完整性与 schema 回归测试。

Reference tests:
- Reference integrity validated via CaseData.entity_by_id(ref) + isinstance kind match.
- ID prefix is just a naming convention; not used as the validation source.

（Phase 2 起，legacy `_normalize_extracted_unit` 适配器已删除，相关语义回归
用例迁移至 tests/test_extraction_contract.py，改为直接针对 typed parser / materializer。）
"""

from datetime import datetime

import pytest

from app.schemas import (
    CaseData,
    Claim,
    Event,
    EventTime,
    Location,
    Object,
    Person,
    RecommendedResolution,
)
from app.case_service import _validate_references


NOW = datetime(2024, 1, 1)


def _empty_case() -> CaseData:
    return CaseData(case_id="case-1", created_at=NOW, updated_at=NOW)


# ---------------------------------------------------------------------------
# Reference integrity: by entity_by_id + isinstance, not by prefix alone.
# ---------------------------------------------------------------------------


class TestValidateReferences:
    def test_valid_event_with_person_actor_and_location(self):
        c = _empty_case()
        c.persons.append(Person(id="person-a", name="张三", created_at=NOW, updated_at=NOW, source_refs=[], confidence="medium", evidence_quote=""))
        c.locations.append(Location(id="location-a", name="客厅", created_at=NOW, updated_at=NOW, source_refs=[], confidence="medium", evidence_quote=""))
        c.events.append(Event(id="event-a", description="x", participant_refs=["person-a"], location_ref="location-a", created_at=NOW, updated_at=NOW, source_refs=[], confidence="medium", evidence_quote=""))
        assert _validate_references(c) == []

    def test_event_actor_ref_to_existing_object_is_invalid(self):
        """ref 存在但 instance type 不对（Object → participant），应判定 invalid。"""
        c = _empty_case()
        c.objects.append(Object(id="object-a", name="刀", created_at=NOW, updated_at=NOW, source_refs=[], confidence="medium", evidence_quote=""))
        c.events.append(Event(id="event-a", description="x", participant_refs=["object-a"], created_at=NOW, updated_at=NOW, source_refs=[], confidence="medium", evidence_quote=""))
        invalid = _validate_references(c)
        assert any("participant_ref" in item for item in invalid)

    def test_event_actor_ref_to_nonexistent_id_is_invalid(self):
        c = _empty_case()
        c.events.append(Event(id="event-a", description="x", participant_refs=["person-missing"], created_at=NOW, updated_at=NOW, source_refs=[], confidence="medium", evidence_quote=""))
        invalid = _validate_references(c)
        assert any("participant_ref" in item for item in invalid)

    def test_event_location_ref_to_existing_object_is_invalid(self):
        c = _empty_case()
        c.objects.append(Object(id="object-a", name="x", created_at=NOW, updated_at=NOW, source_refs=[], confidence="medium", evidence_quote=""))
        c.events.append(Event(id="event-a", description="x", location_ref="object-a", created_at=NOW, updated_at=NOW, source_refs=[], confidence="medium", evidence_quote=""))
        invalid = _validate_references(c)
        assert any("location_ref" in item for item in invalid)

    def test_event_object_ref_to_existing_location_is_invalid(self):
        c = _empty_case()
        c.locations.append(Location(id="location-a", name="客厅", created_at=NOW, updated_at=NOW, source_refs=[], confidence="medium", evidence_quote=""))
        c.events.append(Event(id="event-a", description="x", object_refs=["location-a"], created_at=NOW, updated_at=NOW, source_refs=[], confidence="medium", evidence_quote=""))
        invalid = _validate_references(c)
        assert any("object_ref" in item for item in invalid)

    def test_claim_claimant_ref_to_existing_object_is_invalid(self):
        c = _empty_case()
        c.objects.append(Object(id="object-a", name="刀", created_at=NOW, updated_at=NOW, source_refs=[], confidence="medium", evidence_quote=""))
        c.claims.append(Claim(id="claim-a", proposition="x", claimant_ref="object-a", created_at=NOW, updated_at=NOW, source_refs=[], confidence="medium", evidence_quote=""))
        invalid = _validate_references(c)
        assert any("claimant_ref" in item for item in invalid)

    def test_claim_claimant_ref_none_is_valid(self):
        """claimant_ref=None 表示身份未解析，已存 claimant_text；不应判 invalid。"""
        c = _empty_case()
        c.claims.append(Claim(id="claim-a", proposition="x", claimant_ref=None, claimant_text="某人", created_at=NOW, updated_at=NOW, source_refs=[], confidence="medium", evidence_quote=""))
        assert _validate_references(c) == []

    def test_claim_about_ref_to_nonexistent_id_is_invalid(self):
        c = _empty_case()
        c.persons.append(Person(id="person-a", name="x", created_at=NOW, updated_at=NOW, source_refs=[], confidence="medium", evidence_quote=""))
        c.claims.append(Claim(id="claim-a", proposition="x", claimant_ref="person-a", about_refs=["info-1"], created_at=NOW, updated_at=NOW, source_refs=[], confidence="medium", evidence_quote=""))
        invalid = _validate_references(c)
        assert any("about_ref" in item for item in invalid)

    def test_claim_about_refs_can_point_to_any_existing_kind(self):
        """about_refs 允许指向任意 entity kind，只要 ref 实际存在于 case。"""
        c = _empty_case()
        c.persons.append(Person(id="person-a", name="x", created_at=NOW, updated_at=NOW, source_refs=[], confidence="medium", evidence_quote=""))
        c.events.append(Event(id="event-a", description="x", created_at=NOW, updated_at=NOW, source_refs=[], confidence="medium", evidence_quote=""))
        c.objects.append(Object(id="object-a", name="刀", created_at=NOW, updated_at=NOW, source_refs=[], confidence="medium", evidence_quote=""))
        c.claims.append(Claim(id="claim-a", proposition="x", claimant_ref="person-a",
                              about_refs=["person-a", "event-a", "object-a"],
                              created_at=NOW, updated_at=NOW, source_refs=[], confidence="medium", evidence_quote=""))
        assert _validate_references(c) == []

    def test_claim_typed_fields_valid_refs(self):
        """Claim 强类型字段（participant/location/object）指向正确 kind 时合法。"""
        c = _empty_case()
        c.persons.append(Person(id="person-a", name="王强", created_at=NOW, updated_at=NOW, source_refs=[], confidence="medium", evidence_quote=""))
        c.locations.append(Location(id="location-a", name="仓库", created_at=NOW, updated_at=NOW, source_refs=[], confidence="medium", evidence_quote=""))
        c.objects.append(Object(id="object-a", name="刀", created_at=NOW, updated_at=NOW, source_refs=[], confidence="medium", evidence_quote=""))
        c.claims.append(Claim(id="claim-a", proposition="x", claimant_ref="person-a",
                              participant_refs=["person-a"], location_ref="location-a",
                              object_refs=["object-a"],
                              created_at=NOW, updated_at=NOW, source_refs=[], confidence="medium", evidence_quote=""))
        assert _validate_references(c) == []

    def test_claim_participant_ref_to_object_is_invalid(self):
        """participant_refs 指向 Object → invalid。"""
        c = _empty_case()
        c.objects.append(Object(id="object-a", name="刀", created_at=NOW, updated_at=NOW, source_refs=[], confidence="medium", evidence_quote=""))
        c.claims.append(Claim(id="claim-a", proposition="x", participant_refs=["object-a"],
                              created_at=NOW, updated_at=NOW, source_refs=[], confidence="medium", evidence_quote=""))
        invalid = _validate_references(c)
        assert any("participant_ref" in item for item in invalid)

    def test_claim_participant_ref_to_nonexistent_is_invalid(self):
        c = _empty_case()
        c.claims.append(Claim(id="claim-a", proposition="x", participant_refs=["person-missing"],
                              created_at=NOW, updated_at=NOW, source_refs=[], confidence="medium", evidence_quote=""))
        invalid = _validate_references(c)
        assert any("participant_ref" in item for item in invalid)

    def test_claim_location_ref_to_person_is_invalid(self):
        c = _empty_case()
        c.persons.append(Person(id="person-a", name="x", created_at=NOW, updated_at=NOW, source_refs=[], confidence="medium", evidence_quote=""))
        c.claims.append(Claim(id="claim-a", proposition="x", location_ref="person-a",
                              created_at=NOW, updated_at=NOW, source_refs=[], confidence="medium", evidence_quote=""))
        invalid = _validate_references(c)
        assert any("location_ref" in item for item in invalid)

    def test_claim_object_ref_to_location_is_invalid(self):
        c = _empty_case()
        c.locations.append(Location(id="location-a", name="仓库", created_at=NOW, updated_at=NOW, source_refs=[], confidence="medium", evidence_quote=""))
        c.claims.append(Claim(id="claim-a", proposition="x", object_refs=["location-a"],
                              created_at=NOW, updated_at=NOW, source_refs=[], confidence="medium", evidence_quote=""))
        invalid = _validate_references(c)
        assert any("object_ref" in item for item in invalid)

    def test_claim_time_accepts_event_time(self):
        """Claim.time 复用 EventTime（同一 contract），可正常序列化为 dict。"""
        c = _empty_case()
        c.claims.append(Claim(id="claim-a", proposition="x", claimant_text="某人",
                              time=EventTime(label="2024年3月12日22:10", sort_value="2024-03-12T22:10", precision="exact"),
                              created_at=NOW, updated_at=NOW, source_refs=[], confidence="medium", evidence_quote=""))
        assert _validate_references(c) == []
        assert c.claims[0].time.label == "2024年3月12日22:10"
        assert c.claims[0].time.sort_value == "2024-03-12T22:10"


# ---------------------------------------------------------------------------
# RecommendedResolution：Phase 1b.1 删除 preferred_side
# ---------------------------------------------------------------------------


class TestNoPreferredSide:
    def test_schema_field_absent(self):
        """RecommendedResolution schema 不应暴露 preferred_side 字段。"""
        assert "preferred_side" not in RecommendedResolution.model_fields
        # 实例化也不应接受该字段
        with pytest.raises(Exception):
            RecommendedResolution(recommended_decision="pending", preferred_side="neutral")
