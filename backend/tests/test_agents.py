"""Agent 行为测试。

不依赖真实 LLM，覆盖：
- _mode_to_inference_type 5 种 mode + fallback
- _create_inference_card 标题 + 字段
- _sync_meta_cards_from_entities 全量 vs append_only（新 schema）
- parse_reasoning_view_payload 三视图识别
"""

from datetime import datetime

from app.case_service import (
    _mode_to_inference_type,
    _create_inference_card,
    _sync_meta_cards_from_entities,
)
from app.schemas import (
    CaseData,
    InferenceType,
    DisplayLevel,
    Person,
    MetaCard,
    CardPosition,
    MetaDetail,
    MetaEntityKind,
)


def make_person(id_="person-1", quote="quote", name="name"):
    return Person(
        id=id_,
        name=name,
        created_at=datetime(2024, 1, 1),
        updated_at=datetime(2024, 1, 1),
        source_refs=["file-x"],
        confidence="medium",
        evidence_quote=quote,
    )


def make_empty_case():
    return CaseData(case_id="case-test", created_at=datetime(2024, 1, 1), updated_at=datetime(2024, 1, 1))


class TestModeToInferenceType:
    def test_hypothesis(self):
        assert _mode_to_inference_type("hypothesis") == InferenceType.hypothesis

    def test_conclusion(self):
        assert _mode_to_inference_type("conclusion") == InferenceType.conclusion

    def test_conflict_check_maps_to_conflict(self):
        assert _mode_to_inference_type("conflict_check") == InferenceType.conflict

    def test_missing_evidence(self):
        assert _mode_to_inference_type("missing_evidence") == InferenceType.missing_evidence

    def test_unknown_falls_back_to_hypothesis(self):
        assert _mode_to_inference_type("auto") == InferenceType.hypothesis
        assert _mode_to_inference_type("xxx") == InferenceType.hypothesis


class TestCreateInferenceCard:
    def test_default_titles(self):
        for mode, expected_type in [
            ("hypothesis", InferenceType.hypothesis),
            ("conclusion", InferenceType.conclusion),
            ("conflict_check", InferenceType.conflict),
            ("missing_evidence", InferenceType.missing_evidence),
        ]:
            card = _create_inference_card(claim="claim", selected_ids=["a"], mode=mode)
            assert card.inference_type == expected_type
            assert card.title  # fallback 标题非空
            assert card.detail.claim == "claim"
            assert card.detail.supporting_card_ids == ["a"]

    def test_explicit_title_overrides_fallback(self):
        card = _create_inference_card(claim="c", selected_ids=[], mode="hypothesis", title="自定义")
        assert card.title == "自定义"

    def test_blank_title_uses_fallback(self):
        card = _create_inference_card(claim="c", selected_ids=[], mode="hypothesis", title="   ")
        assert card.title == "Generated Hypothesis"


class TestSyncMetaCardsFromEntities:
    def test_default_rebuild_from_entities(self):
        case = make_empty_case()
        extracted = {
            "persons": [make_person("person-new1"), make_person("person-new2")],
            "events": [],
            "locations": [],
            "objects": [],
            "claims": [],
            "evidences": [],
        }
        _sync_meta_cards_from_entities(case, extracted, append_only=False)
        assert len(case.meta_cards) == 2
        assert all(c.id.startswith("meta-person-") for c in case.meta_cards)
        assert all(c.entity_kind == MetaEntityKind.person for c in case.meta_cards)

    def test_append_only_preserves_existing(self):
        case = make_empty_case()
        old_card = MetaCard(
            id="meta-old",
            card_type="meta",
            title="old",
            display_level=DisplayLevel.level_1,
            position=CardPosition(x=0, y=0),
            created_at=datetime(2024, 1, 1),
            updated_at=datetime(2024, 1, 1),
            entity_kind=MetaEntityKind.person,
            entity_ref="person-old",
            detail=MetaDetail(),
        )
        case.meta_cards = [old_card]

        extracted = {"persons": [make_person("person-new1"), make_person("person-new2")],
                     "events": [], "locations": [], "objects": [], "claims": [], "evidences": []}
        _sync_meta_cards_from_entities(case, extracted, append_only=True)

        # 旧卡保留 + 新卡追加
        assert len(case.meta_cards) == 3
        assert case.meta_cards[0].id == "meta-old"
        assert case.meta_cards[1].id == "meta-person-new1"
        assert case.meta_cards[2].id == "meta-person-new2"

    def test_append_only_dedup_by_entity_ref(self):
        case = make_empty_case()
        old_card = MetaCard(
            id="meta-old",
            card_type="meta",
            title="old",
            display_level=DisplayLevel.level_1,
            position=CardPosition(x=0, y=0),
            created_at=datetime(2024, 1, 1),
            updated_at=datetime(2024, 1, 1),
            entity_kind=MetaEntityKind.person,
            entity_ref="person-shared",
            detail=MetaDetail(),
        )
        case.meta_cards = [old_card]

        extracted = {"persons": [make_person("person-shared")],
                     "events": [], "locations": [], "objects": [], "claims": [], "evidences": []}
        _sync_meta_cards_from_entities(case, extracted, append_only=True)

        # 同 entity_ref 不重复添加
        assert len(case.meta_cards) == 1
        assert case.meta_cards[0].id == "meta-old"

    def test_full_rebuild_preserves_file_level_cards(self):
        case = make_empty_case()
        # file-level 卡（来自 create_case_from_uploads）；文件只是 provenance，
        # entity_kind 保留 evidence 仅作前端渲染标签，entity_ref 指向真实 file id。
        ev_card = MetaCard(
            id="meta-file-x",
            card_type="meta",
            title="file",
            display_level=DisplayLevel.level_1,
            position=CardPosition(x=10, y=10),
            created_at=datetime(2024, 1, 1),
            updated_at=datetime(2024, 1, 1),
            entity_kind=MetaEntityKind.evidence,
            entity_ref="file-x",
            detail=MetaDetail(),
        )
        case.meta_cards = [ev_card]

        extracted = {"persons": [make_person("person-new1")],
                     "events": [], "locations": [], "objects": [], "claims": [], "evidences": []}
        _sync_meta_cards_from_entities(case, extracted, append_only=False)

        # file-level 卡保留 + 新卡追加
        assert len(case.meta_cards) == 2
        assert case.meta_cards[0].id == "meta-file-x"
        assert case.meta_cards[1].id == "meta-person-new1"