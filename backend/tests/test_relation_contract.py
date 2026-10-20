"""Phase 1c relation contract 测试。

覆盖：
1. structural projection（build_structural_edges）：
   - 从 core entity refs deterministic 投影 claimed_by/involved_in/about/occurs_in/involves_object。
   - 严格 ref 校验：dangling ref 或类型不匹配 → 抛 ReferenceIntegrityError，不静默缺边。
2. semantic projection（build_semantic_edges / build_graph）。
3. _build_semantic_relations 端点约束 + contradicts 对称 canonical dedup。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from app.case_service import (
    ReferenceIntegrityError,
    _build_semantic_relations,
    build_graph,
    build_semantic_edges,
    build_structural_edges,
)
from app.schemas import (
    CaseData,
    Claim,
    Event,
    Evidence,
    Location,
    Object,
    Person,
    SemanticRelation,
    SemanticRelationType,
)

NOW = datetime(2024, 1, 1)


def _case() -> CaseData:
    return CaseData(case_id="case-1", created_at=NOW, updated_at=NOW)


def _person(id_="person-a", name="张三"):
    return Person(id=id_, name=name, created_at=NOW, updated_at=NOW, source_refs=[], confidence="medium", evidence_quote="")


def _location(id_="location-a", name="客厅"):
    return Location(id=id_, name=name, created_at=NOW, updated_at=NOW, source_refs=[], confidence="medium", evidence_quote="")


def _object(id_="object-a", name="刀"):
    return Object(id=id_, name=name, created_at=NOW, updated_at=NOW, source_refs=[], confidence="medium", evidence_quote="")


def _claim(id_="claim-a", claimant=None, about=None, proposition="p"):
    return Claim(id=id_, proposition=proposition, claimant_ref=claimant, about_refs=about or [],
                 created_at=NOW, updated_at=NOW, source_refs=[], confidence="medium", evidence_quote="")


def _event(id_="event-a", participants=None, location=None, objects=None):
    return Event(id=id_, description="e", participant_refs=participants or [], location_ref=location,
                 object_refs=objects or [], created_at=NOW, updated_at=NOW, source_refs=[], confidence="medium", evidence_quote="")


def _evidence(id_="evidence-a", about=None):
    return Evidence(id=id_, description="ev", about_refs=about or [], created_at=NOW, updated_at=NOW,
                    source_refs=[], confidence="medium", evidence_quote="")


# ---------------------------------------------------------------------------
# structural projection
# ---------------------------------------------------------------------------


class TestBuildStructuralEdges:
    def test_full_projection_types(self):
        case = _case()
        case.persons.append(_person())
        case.locations.append(_location())
        case.objects.append(_object())
        case.claims.append(_claim(claimant="person-a", about=["event-a"]))
        case.evidences.append(_evidence(about=["claim-a"]))
        case.events.append(_event(participants=["person-a"], location="location-a", objects=["object-a"]))

        edges = build_structural_edges(case)
        types = {e.edge_type.value for e in edges}
        assert types == {"about", "claimed_by", "involves_object", "involved_in", "occurs_in"}

    def test_empty_case_yields_no_edges(self):
        assert build_structural_edges(_case()) == []

    def test_dangling_claimant_ref_raises(self):
        """claimant_ref 指向不存在的 Person → ReferenceIntegrityError，不静默 skip。"""
        case = _case()
        case.claims.append(_claim(claimant="person-missing"))
        with pytest.raises(ReferenceIntegrityError):
            build_structural_edges(case)

    def test_type_mismatched_location_ref_raises(self):
        """location_ref 指向 Object（类型不匹配）→ ReferenceIntegrityError。"""
        case = _case()
        case.objects.append(_object())
        case.events.append(_event(location="object-a"))
        with pytest.raises(ReferenceIntegrityError):
            build_structural_edges(case)

    def test_dangling_about_ref_raises(self):
        case = _case()
        case.claims.append(_claim(about=["nonexistent"]))
        with pytest.raises(ReferenceIntegrityError):
            build_structural_edges(case)

    def test_participant_ref_to_object_raises(self):
        case = _case()
        case.objects.append(_object())
        case.events.append(_event(participants=["object-a"]))
        with pytest.raises(ReferenceIntegrityError):
            build_structural_edges(case)


# ---------------------------------------------------------------------------
# semantic projection
# ---------------------------------------------------------------------------


class TestBuildSemanticEdges:
    def test_projection_maps_supports_and_contradicts(self):
        case = _case()
        case.semantic_relations = [
            SemanticRelation(id="rel-1", source_id="claim-a", target_id="claim-b",
                             relation_type=SemanticRelationType.supports, created_at=NOW),
            SemanticRelation(id="rel-2", source_id="claim-c", target_id="claim-d",
                             relation_type=SemanticRelationType.contradicts, created_at=NOW),
        ]
        edges = build_semantic_edges(case)
        assert sorted(e.edge_type.value for e in edges) == ["contradicts", "supports"]

    def test_build_graph_merges_structural_and_semantic(self):
        case = _case()
        case.persons.append(_person())
        case.claims.append(_claim(claimant="person-a"))
        case.claims.append(_claim(id_="claim-b", proposition="p2"))
        case.semantic_relations = [
            SemanticRelation(id="rel-1", source_id="claim-a", target_id="claim-b",
                             relation_type=SemanticRelationType.supports, created_at=NOW),
        ]
        edges = build_graph(case)
        assert sorted(e.edge_type.value for e in edges) == ["claimed_by", "supports"]


# ---------------------------------------------------------------------------
# semantic relation 端点约束 + dedup
# ---------------------------------------------------------------------------


class TestBuildSemanticRelations:
    def test_supports_claim_to_claim(self):
        case = _case()
        case.claims.append(_claim(id_="claim-a"))
        case.claims.append(_claim(id_="claim-b"))
        rels = _build_semantic_relations(case, [
            {"source_id": "claim-a", "target_id": "claim-b", "relation_type": "supports"},
        ])
        assert len(rels) == 1
        assert rels[0].relation_type == SemanticRelationType.supports

    def test_supports_evidence_to_claim(self):
        case = _case()
        case.claims.append(_claim(id_="claim-a"))
        case.evidences.append(_evidence())
        rels = _build_semantic_relations(case, [
            {"source_id": "evidence-a", "target_id": "claim-a", "relation_type": "supports"},
        ])
        assert len(rels) == 1

    def test_supports_evidence_to_evidence_rejected(self):
        """第一版：禁止 Evidence → Evidence 的 supports。"""
        case = _case()
        case.evidences.append(_evidence(id_="evidence-a"))
        case.evidences.append(_evidence(id_="evidence-b"))
        rels = _build_semantic_relations(case, [
            {"source_id": "evidence-a", "target_id": "evidence-b", "relation_type": "supports"},
        ])
        assert rels == []

    def test_supports_target_must_be_claim(self):
        """supports target 不是 Claim（如 Event）→ 丢弃。"""
        case = _case()
        case.events.append(_event())
        case.claims.append(_claim(id_="claim-a"))
        rels = _build_semantic_relations(case, [
            {"source_id": "claim-a", "target_id": "event-a", "relation_type": "supports"},
        ])
        assert rels == []

    def test_contradicts_claim_to_claim(self):
        case = _case()
        case.claims.append(_claim(id_="claim-a"))
        case.claims.append(_claim(id_="claim-b"))
        rels = _build_semantic_relations(case, [
            {"source_id": "claim-a", "target_id": "claim-b", "relation_type": "contradicts"},
        ])
        assert len(rels) == 1
        assert rels[0].relation_type == SemanticRelationType.contradicts

    def test_contradicts_symmetric_canonical_dedup(self):
        """A↔B 与 B↔A 只保留一条。"""
        case = _case()
        case.claims.append(_claim(id_="claim-a"))
        case.claims.append(_claim(id_="claim-b"))
        rels = _build_semantic_relations(case, [
            {"source_id": "claim-a", "target_id": "claim-b", "relation_type": "contradicts"},
            {"source_id": "claim-b", "target_id": "claim-a", "relation_type": "contradicts"},
        ])
        assert len(rels) == 1

    def test_contradicts_evidence_rejected(self):
        case = _case()
        case.evidences.append(_evidence(id_="evidence-a"))
        case.claims.append(_claim(id_="claim-a"))
        rels = _build_semantic_relations(case, [
            {"source_id": "evidence-a", "target_id": "claim-a", "relation_type": "contradicts"},
        ])
        assert rels == []

    def test_invalid_relation_type_dropped(self):
        case = _case()
        case.claims.append(_claim(id_="claim-a"))
        case.claims.append(_claim(id_="claim-b"))
        rels = _build_semantic_relations(case, [
            {"source_id": "claim-a", "target_id": "claim-b", "relation_type": "claimed_by"},
        ])
        assert rels == []

    def test_dangling_ref_dropped(self):
        case = _case()
        case.claims.append(_claim(id_="claim-a"))
        rels = _build_semantic_relations(case, [
            {"source_id": "claim-a", "target_id": "claim-missing", "relation_type": "supports"},
        ])
        assert rels == []


# ---------------------------------------------------------------------------
# relation prompt 契约守卫
# ---------------------------------------------------------------------------


class TestRelationPromptContract:
    def _prompt_text(self) -> str:
        path = Path(__file__).resolve().parent.parent / "prompts" / "relation_system_prompt.md"
        return path.read_text(encoding="utf-8")

    def test_contradicts_dedup_not_llm_responsibility(self):
        """contradicts 去重由 service 层负责，prompt 不得要求 LLM 决定持久化方向。"""
        text = self._prompt_text()
        assert "方向取先出现者" not in text
        assert "service 层" in text

    def test_evidence_quote_contract_preserved(self):
        """runtime（_compact_entity_for_relation）确实传 evidence_quote，prompt 保持一致。"""
        text = self._prompt_text()
        assert "evidence_quote" in text

    def test_taxonomy_unchanged(self):
        text = self._prompt_text()
        assert "supports" in text
        assert "contradicts" in text
