"""Phase 1b schema 校验测试。

覆盖 Core Info Model：
- 6 类 type-specific entity 基本构造与必填字段
- MetaCard 用 entity_kind/entity_ref 替代 info_type/info_subtype
- CaseData 拆分为 6 个 entity 集合 + entity_by_id/all_entities helpers
- 旧枚举 / 旧字段已删除（不再 import InfoUnitType 等）
"""

from datetime import datetime

import pytest

from app.schemas import (
    CaseData,
    Claim,
    Event,
    EventTime,
    Evidence,
    GraphEdge,
    GraphEdgeType,
    MetaCard,
    MetaDetail,
    MetaEntityKind,
    Person,
    RecommendedView,
    SemanticRelation,
    SemanticRelationType,
    ConflictViewPayload,
    HypothesisViewPayload,
    parse_reasoning_view_payload,
)


NOW = datetime(2024, 1, 1)


def _person(id_="person-a", name="张三", **kwargs):
    return Person(
        id=id_, name=name, created_at=NOW, updated_at=NOW,
        source_refs=kwargs.pop("source_refs", []),
        confidence=kwargs.pop("confidence", "medium"),
        evidence_quote=kwargs.pop("evidence_quote", "他说他是张三"),
        description=kwargs.pop("description", None),
        **kwargs,
    )


def _event(id_="event-a", desc="某事", participants=None, location=None, time=None, objects=None):
    return Event(
        id=id_, description=desc, created_at=NOW, updated_at=NOW,
        participant_refs=participants or [], location_ref=location, time=time,
        object_refs=objects or [],
        source_refs=[], confidence="medium", evidence_quote="",
    )


def _claim(id_="claim-a", proposition="...", speaker="person-a", about=None):
    return Claim(
        id=id_, proposition=proposition, claimant_ref=speaker,
        about_refs=about or [], source_refs=[], confidence="medium",
        evidence_quote="", created_at=NOW, updated_at=NOW,
    )


def _evidence(id_="evidence-a", desc="材料", about=None):
    return Evidence(
        id=id_, description=desc, about_refs=about or [], source_refs=[],
        confidence="medium", evidence_quote="", created_at=NOW, updated_at=NOW,
    )


# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------


class TestEventTimeStructure:
    def test_minimal_fields(self):
        t = EventTime(label="三天后", sort_value=None, precision="approximate")
        assert t.label == "三天后"
        assert t.sort_value is None
        assert t.precision == "approximate"

    def test_no_relative_time_fields(self):
        """relative_to / relation / offset 已从 contract 移除。"""
        t = EventTime(label="次日", sort_value=None)
        assert not hasattr(t, "relative_to")
        assert not hasattr(t, "relation")
        assert not hasattr(t, "offset")

    def test_canonical_sort_value_accepted(self):
        t = EventTime(label="2024年3月12日20:00", sort_value="2024-03-12T20:00", precision="exact")
        assert t.sort_value == "2024-03-12T20:00"

    def test_sort_value_defaults_to_none(self):
        t = EventTime(label="当晚")
        assert t.sort_value is None
        assert t.precision == "unknown"


class TestEntityModels:
    def test_person_only_has_person_fields(self):
        p = _person()
        assert hasattr(p, "name")
        assert hasattr(p, "description")
        assert hasattr(p, "aliases")  # Phase 1c：schema support
        # 不持有 claimant_ref / participant_refs 等其他 entity 专属字段
        assert not hasattr(p, "claimant_ref")
        assert not hasattr(p, "participant_refs")

    def test_event_only_has_event_fields(self):
        e = _event()
        assert hasattr(e, "time")
        assert hasattr(e, "participant_refs")
        assert hasattr(e, "location_ref")
        assert hasattr(e, "object_refs")
        # 没有 name / proposition / description 不应作为 Person-style name
        assert not hasattr(e, "claimant_ref")

    def test_claim_claimant_ref_optional_with_claimant_text_provenance(self):
        """Phase 1c：claimant_ref 改为可选（None）；claimant 无法解析时填 claimant_text 保留 provenance。"""
        c = Claim(id="claim-x", proposition="x", source_refs=[], confidence="medium",
                  evidence_quote="", created_at=NOW, updated_at=NOW,
                  claimant_ref=None, claimant_text="某人")
        assert c.claimant_ref is None
        assert c.claimant_text == "某人"

    def test_evidence_only_has_about_refs(self):
        ev = _evidence()
        assert hasattr(ev, "about_refs")
        assert not hasattr(ev, "claimant_ref")
        assert not hasattr(ev, "participant_refs")


class TestMetaCardEntityRef:
    def test_meta_card_uses_entity_kind_and_ref(self):
        card = MetaCard(
            id="meta-x",
            title="x",
            summary="y",
            display_level=1,
            position={"x": 0, "y": 0},
            created_at=NOW, updated_at=NOW,
            entity_kind=MetaEntityKind.person,
            entity_ref="person-a",
            detail=MetaDetail(),
        )
        assert card.entity_kind == MetaEntityKind.person
        assert card.entity_ref == "person-a"
        # 不再有 info_type / info_subtype
        assert not hasattr(card, "info_type")
        assert not hasattr(card, "info_subtype")


class TestCaseDataEntityLists:
    def test_empty_case_has_all_entity_lists(self):
        c = CaseData(case_id="case-x", created_at=NOW, updated_at=NOW)
        assert c.persons == []
        assert c.events == []
        assert c.locations == []
        assert c.objects == []
        assert c.claims == []
        assert c.evidences == []
        # 旧 info_units 已删除
        assert not hasattr(c, "info_units")
        # 旧 core_issue / issue_set 已删除
        assert not hasattr(c, "core_issue")
        assert not hasattr(c, "issue_set")
        # Phase 1c：legacy graph 已删除（edges / relations 不再是 persistence source）
        assert not hasattr(c, "edges")
        assert not hasattr(c, "relations")
        # Phase 1c：semantic relations 持久化，默认为空
        assert c.semantic_relations == []

    def test_entity_by_id_helpers(self):
        c = CaseData(case_id="case-x", created_at=NOW, updated_at=NOW)
        c.persons.append(_person())
        c.events.append(_event())
        c.claims.append(_claim())

        assert isinstance(c.entity_by_id("person-a"), Person)
        assert isinstance(c.entity_by_id("event-a"), Event)
        assert isinstance(c.entity_by_id("claim-a"), Claim)
        assert c.entity_by_id("nonexistent") is None

        all_e = c.all_entities()
        assert len(all_e) == 3
        kinds = {type(e).__name__ for e in all_e}
        assert kinds == {"Person", "Event", "Claim"}


# ---------------------------------------------------------------------------
# Phase 1c：SemanticRelation / GraphEdgeType / SemanticRelationType / aliases
# ---------------------------------------------------------------------------


class TestSemanticRelationSchema:
    def test_relation_type_rejects_structural_edge_type(self):
        """SemanticRelation.relation_type 在类型层就不可能接受 claimed_by 等 structural edge type。"""
        with pytest.raises(Exception):
            SemanticRelation(
                id="rel-1", source_id="claim-a", target_id="claim-b",
                relation_type="claimed_by",  # structural type，非法
                created_at=NOW,
            )

    def test_relation_accepts_supports_and_contradicts(self):
        for rt in ("supports", "contradicts"):
            rel = SemanticRelation(
                id="rel-1", source_id="claim-a", target_id="claim-b",
                relation_type=rt, created_at=NOW,
            )
            assert rel.relation_type == SemanticRelationType(rt)

    def test_detection_confidence_field_exists(self):
        """Phase 1c：字段名是 detection_confidence，不是裸 confidence。"""
        rel = SemanticRelation(
            id="rel-1", source_id="claim-a", target_id="claim-b",
            relation_type="supports", detection_confidence="high", created_at=NOW,
        )
        assert rel.detection_confidence.value == "high"
        assert "detection_confidence" in SemanticRelation.model_fields
        assert "confidence" not in SemanticRelation.model_fields

    def test_evidence_basis_field_absent(self):
        """SemanticRelation 不持久化 evidence_basis：source/target 已能定位原始 prose。"""
        assert "evidence_basis" not in SemanticRelation.model_fields


class TestGraphEdgeType:
    def test_seven_values(self):
        assert [e.value for e in GraphEdgeType] == [
            "claimed_by", "involved_in", "about", "occurs_in", "involves_object",
            "supports", "contradicts",
        ]

    def test_graph_edge_uses_graph_edge_type(self):
        edge = GraphEdge(
            id="edge-1", source="a", target="b", edge_type="supports", created_at=NOW,
        )
        assert edge.edge_type == GraphEdgeType.supports


class TestPersonAliases:
    def test_aliases_default_empty(self):
        p = _person()
        assert p.aliases == []

    def test_aliases_are_plain_strings(self):
        p = _person(aliases=["老陈", "陈建国"])
        assert p.aliases == ["老陈", "陈建国"]


# ---------------------------------------------------------------------------
# Reasoning view payload tests (unchanged interface)
# ---------------------------------------------------------------------------


class TestReasoningViewPayload:
    def test_conflict_payload(self):
        payload = ConflictViewPayload(title="冲突")
        parsed = parse_reasoning_view_payload(payload.model_dump())
        assert isinstance(parsed, ConflictViewPayload)
        assert parsed.view_type == "conflict"

    def test_hypothesis_payload(self):
        payload = HypothesisViewPayload()
        parsed = parse_reasoning_view_payload(payload.model_dump())
        assert isinstance(parsed, HypothesisViewPayload)
        assert parsed.view_type == "hypothesis"

    def test_timeline_payload_no_longer_reasoning_view(self):
        """Timeline 已改为 runtime projection View Model，不再是 ReasoningViewPayload 之一。"""
        assert parse_reasoning_view_payload({"view_type": "timeline"}) is None

    def test_invalid_payload_returns_none(self):
        assert parse_reasoning_view_payload({"view_type": "unknown"}) is None
        assert parse_reasoning_view_payload(None) is None
        assert parse_reasoning_view_payload("not a dict") is None


class TestRecommendedView:
    def test_three_values(self):
        assert RecommendedView.timeline.value == "timeline"
        assert RecommendedView.conflict.value == "conflict"
        assert RecommendedView.hypothesis.value == "hypothesis"