"""Phase 2 — Extraction contract 测试。

覆盖：
1. parse_extraction_output：严格 typed 解析 + legacy schema 显式拒绝。
2. materialize_extraction_output：id 全局唯一化 + 引用重写 + source_refs 默认。
3. build_extraction_context：最小 context（不含 case_title / parse_status / 历史 entity）。
4. run_extraction_agent：success / provider error / parser error / reference error。
5. 必测 fixtures A–I 的 parser/materializer 层结构不变量。
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

import pytest

from app.case_service import (
    LLMRequestError,
    build_extraction_context,
    run_extraction_agent,
)
from app.extraction import (
    ExtractionStageError,
    materialize_extraction_output,
    parse_extraction_output,
)
from app.schemas import (
    CaseData,
    CaseFile,
    Event,
    EventTime,
    ExtractionRequest,
    ExtractionLLMResult,
    FileParseStatus,
    FileType,
    ReferenceIntegrityError,
)

NOW = datetime(2024, 1, 1)


def _empty_case() -> CaseData:
    return CaseData(case_id="case-1", created_at=NOW, updated_at=NOW)


def _materialize(raw: dict, file_ids: list[str] | None = None) -> dict[str, list]:
    return materialize_extraction_output(parse_extraction_output(raw), file_ids or ["file-1"])


# ---------------------------------------------------------------------------
# parse_extraction_output：严格 typed 解析 + legacy 拒绝
# ---------------------------------------------------------------------------


class TestParseExtractionOutput:
    def test_accepts_typed_output(self):
        result = parse_extraction_output({"persons": [{"id": "person-1", "name": "李明"}]})
        assert isinstance(result, ExtractionLLMResult)
        assert len(result.persons) == 1
        assert result.persons[0].name == "李明"

    def test_rejects_legacy_info_unit_schema(self):
        """LLM 仍输出旧 InfoUnit 形态 → 显式拒绝，不做兼容适配。"""
        with pytest.raises(ExtractionStageError):
            parse_extraction_output(
                {"core_issue": "x", "issue_set": ["y"], "info_units": [{"type": "claim"}]}
            )

    def test_rejects_non_dict(self):
        with pytest.raises(ExtractionStageError):
            parse_extraction_output([])
        with pytest.raises(ExtractionStageError):
            parse_extraction_output("not a dict")

    def test_rejects_unknown_top_level_field(self):
        """extra="forbid"：顶层未知字段（如 legacy info_units 之外的噪声）也拒绝。"""
        with pytest.raises(ExtractionStageError):
            parse_extraction_output({"persons": [], "unexpected": 1})


# ---------------------------------------------------------------------------
# materialize_extraction_output：id 全局唯一化 + 引用重写 + source_refs 默认
# ---------------------------------------------------------------------------


class TestMaterializeExtractionOutput:
    def test_remaps_local_ids_and_rewrites_refs(self):
        out = _materialize({
            "persons": [{"id": "person-1", "name": "李明"}],
            "events": [{"id": "event-1", "description": "x", "participant_refs": ["person-1"]}],
        })
        person = out["persons"][0]
        event = out["events"][0]
        assert person.id.startswith("person-")
        # 引用已重写为全局 id
        assert event.participant_refs == [person.id]

    def test_remapped_ids_are_globally_unique(self):
        """两次 materialize 相同 local id，产出不同的全局 id（跨批次防冲突）。"""
        out1 = _materialize({"persons": [{"id": "person-1", "name": "甲"}]})
        out2 = _materialize({"persons": [{"id": "person-1", "name": "乙"}]})
        assert out1["persons"][0].id != out2["persons"][0].id

    def test_defaults_empty_source_refs_to_file_ids(self):
        out = _materialize(
            {"persons": [{"id": "person-1", "name": "李明", "source_refs": []}]},
            file_ids=["file-9"],
        )
        assert out["persons"][0].source_refs == ["file-9"]

    def test_preserves_explicit_source_refs(self):
        out = _materialize(
            {"persons": [{"id": "person-1", "name": "李明", "source_refs": ["file-2"]}]},
            file_ids=["file-9"],
        )
        assert out["persons"][0].source_refs == ["file-2"]

    def test_empty_source_refs_with_no_files_stays_empty(self):
        """raw_text 纯文本抽取（无 file）：source_refs=[] 是合法终态，不强制填 file id。"""
        out = materialize_extraction_output(
            parse_extraction_output({"persons": [{"id": "person-1", "name": "李明", "source_refs": []}]}),
            file_ids=[],
        )
        assert out["persons"][0].source_refs == []

    def test_empty_local_id_gets_fresh_global_id(self):
        out = _materialize({"persons": [{"id": "", "name": "李明"}]})
        assert out["persons"][0].id.startswith("person-")


# ---------------------------------------------------------------------------
# build_extraction_context：最小 context
# ---------------------------------------------------------------------------


class TestBuildExtractionContext:
    def test_minimal_context(self):
        case = _empty_case()
        case.files.append(
            CaseFile(
                file_id="file-1",
                filename="旁白.txt",
                file_type=FileType.txt,
                uploaded_at=NOW,
                parse_status=FileParseStatus.parsed,
                preview_text="李明进入仓库。",
            )
        )
        ctx = build_extraction_context(case, ExtractionRequest(case_id="case-1"))

        assert ctx["files"][0]["file_id"] == "file-1"
        assert ctx["files"][0]["content"] == "李明进入仓库。"
        assert "raw_text" in ctx
        assert "context" in ctx
        # 不传无关字段
        assert "case_title" not in ctx
        assert "parse_status" not in ctx["files"][0]
        assert "persons" not in ctx
        assert "events" not in ctx


# ---------------------------------------------------------------------------
# run_extraction_agent：success / error 三态
# ---------------------------------------------------------------------------


def _patch_no_disk(monkeypatch):
    monkeypatch.setattr("app.case_service.append_case_log", lambda *a, **k: None)


def _llm_result(parsed):
    return {
        "parsed_json": parsed,
        "raw_reply": "ok",
        "request_meta": {},
        "response_meta": {"http_status": 200},
    }


class TestRunExtractionAgent:
    def test_success_merges_entities(self, monkeypatch):
        _patch_no_disk(monkeypatch)
        monkeypatch.setattr(
            "app.case_service._call_llm_sync",
            lambda sp, up: _llm_result({"persons": [{"id": "person-1", "name": "李明"}]}),
        )
        case = _empty_case()
        out = asyncio.run(run_extraction_agent(case, ExtractionRequest(case_id="case-1")))

        assert len(out["persons"]) == 1
        assert len(case.persons) == 1
        assert case.persons[0].name == "李明"

    def test_success_merges_extraction_evidence(self, monkeypatch):
        """Extraction 明确返回 Evidence 时，正常保存到 case.evidences（不丢弃）。"""
        _patch_no_disk(monkeypatch)
        monkeypatch.setattr(
            "app.case_service._call_llm_sync",
            lambda sp, up: _llm_result({"evidences": [{"id": "evidence-1", "description": "监控录像"}]}),
        )
        case = _empty_case()
        out = asyncio.run(run_extraction_agent(case, ExtractionRequest(case_id="case-1")))

        assert len(out["evidences"]) == 1
        assert len(case.evidences) == 1
        assert case.evidences[0].description == "监控录像"

    def test_empty_output_is_success_not_error(self, monkeypatch):
        _patch_no_disk(monkeypatch)
        monkeypatch.setattr(
            "app.case_service._call_llm_sync",
            lambda sp, up: _llm_result(
                {"persons": [], "events": [], "locations": [], "objects": [], "claims": [], "evidences": []}
            ),
        )
        case = _empty_case()
        out = asyncio.run(run_extraction_agent(case, ExtractionRequest(case_id="case-1")))
        assert all(len(v) == 0 for v in out.values())

    def test_provider_failure_raises(self, monkeypatch):
        _patch_no_disk(monkeypatch)

        def _boom(sp, up):
            raise LLMRequestError("provider down", status_code=500)

        monkeypatch.setattr("app.case_service._call_llm_sync", _boom)
        case = _empty_case()
        with pytest.raises(ExtractionStageError):
            asyncio.run(run_extraction_agent(case, ExtractionRequest(case_id="case-1")))

    def test_legacy_schema_raises(self, monkeypatch):
        _patch_no_disk(monkeypatch)
        monkeypatch.setattr(
            "app.case_service._call_llm_sync",
            lambda sp, up: _llm_result({"info_units": [{"type": "claim"}]}),
        )
        case = _empty_case()
        with pytest.raises(ExtractionStageError):
            asyncio.run(run_extraction_agent(case, ExtractionRequest(case_id="case-1")))

    def test_invalid_reference_raises_and_does_not_merge(self, monkeypatch):
        _patch_no_disk(monkeypatch)
        monkeypatch.setattr(
            "app.case_service._call_llm_sync",
            lambda sp, up: _llm_result({
                "events": [{"id": "event-1", "description": "x", "participant_refs": ["person-missing"]}],
            }),
        )
        case = _empty_case()
        with pytest.raises(ReferenceIntegrityError):
            asyncio.run(run_extraction_agent(case, ExtractionRequest(case_id="case-1")))
        # 无效 entity 不落盘
        assert case.events == []


# ---------------------------------------------------------------------------
# 必测 fixtures A–I（parser/materializer 层结构不变量）
# ---------------------------------------------------------------------------


class TestFixturesAtoI:
    def test_a_narrator_becomes_event(self):
        out = _materialize({
            "events": [{"id": "event-1", "description": "李明在20:00进入仓库。"}],
        })
        assert len(out["events"]) == 1
        assert isinstance(out["events"][0], Event)
        assert out["events"][0].description == "李明在20:00进入仓库。"

    def test_b_claim_not_upgraded_to_event(self):
        out = _materialize({
            "persons": [{"id": "person-w", "name": "王强"}],
            "claims": [{"id": "claim-1", "claimant_ref": "person-w",
                        "proposition": "我看到李明20点进入仓库。"}],
        })
        assert len(out["claims"]) == 1
        assert out["events"] == []  # 不自动升级为世界 Event

    def test_c_diary_claim_claimant_resolves(self):
        out = _materialize({
            "persons": [{"id": "person-w", "name": "王强"}],
            "claims": [{"id": "claim-1", "claimant_ref": "person-w",
                        "proposition": "昨晚八点我去了仓库。"}],
        })
        assert out["claims"][0].claimant_ref == out["persons"][0].id

    def test_d_cctv_evidence_plus_event_no_wrong_binding(self):
        out = _materialize({
            "evidences": [{"id": "evidence-1", "description": "仓库监控录像"}],
            "events": [{"id": "event-1", "description": "20:03一名外貌与李明相似的人进入仓库。"}],
        })
        assert len(out["evidences"]) == 1
        assert len(out["events"]) == 1
        # 保留限定信息，不 collapse 成"李明"
        assert "外貌与李明相似" in out["events"][0].description
        assert out["events"][0].participant_refs == []
        # 未错误创建 Person "李明"
        assert all(p.name != "李明" for p in out["persons"])

    def test_e_alias_single_person_no_same_as(self):
        out = _materialize({
            "persons": [{"id": "person-1", "name": "陈建国", "aliases": ["老陈"]}],
        })
        assert len(out["persons"]) == 1
        p = out["persons"][0]
        assert p.name == "陈建国"
        assert p.aliases == ["老陈"]
        assert not hasattr(p, "same_as")

    def test_f_multi_participant_event(self):
        out = _materialize({
            "persons": [
                {"id": "person-a", "name": "李明"},
                {"id": "person-b", "name": "王强"},
            ],
            "events": [{"id": "event-1", "description": "李明和王强发生争执。",
                        "participant_refs": ["person-a", "person-b"]}],
        })
        ev = out["events"][0]
        assert len(ev.participant_refs) == 2
        assert set(ev.participant_refs) == {out["persons"][0].id, out["persons"][1].id}

    def test_g_claim_entities_no_ownership(self):
        out = _materialize({
            "persons": [
                {"id": "person-w", "name": "王强"},
                {"id": "person-l", "name": "李明"},
            ],
            "objects": [{"id": "object-k", "name": "刀"}],
            "claims": [{"id": "claim-1", "claimant_ref": "person-w",
                        "proposition": "李明拿着刀。", "about_refs": ["person-l", "object-k"]}],
        })
        assert {p.name for p in out["persons"]} == {"王强", "李明"}
        assert {o.name for o in out["objects"]} == {"刀"}
        assert len(out["claims"]) == 1
        # 不自动构造 ownership / possession / 持刀 Event
        assert out["events"] == []
        assert set(out["claims"][0].about_refs) == {out["persons"][1].id, out["objects"][0].id}

    def test_h_uncertainty_preserved(self):
        out = _materialize({
            "events": [{"id": "event-1", "description": "李明似乎没有注意到钥匙。"}],
        })
        assert out["events"][0].description == "李明似乎没有注意到钥匙。"
        assert "似乎" in out["events"][0].description

    def test_i_contradictory_claims_keep_confidence(self):
        out = _materialize({
            "claims": [
                {"id": "claim-a", "proposition": "灯亮着", "confidence": "high"},
                {"id": "claim-b", "proposition": "灯没亮", "confidence": "low"},
            ],
        })
        conf = {c.proposition: c.confidence.value for c in out["claims"]}
        assert conf["灯亮着"] == "high"
        assert conf["灯没亮"] == "low"
        # extraction 阶段不建立 contradicts
        assert len(out["claims"]) == 2


# ---------------------------------------------------------------------------
# prompt 契约守卫：不再出现 legacy InfoUnit
# ---------------------------------------------------------------------------


class TestPromptContract:
    def _prompt_text(self) -> str:
        path = Path(__file__).resolve().parent.parent / "prompts" / "extraction_system_prompt.md"
        return path.read_text(encoding="utf-8")

    def test_declares_six_entity_keys(self):
        text = self._prompt_text()
        for key in ("persons", "events", "locations", "objects", "claims", "evidences"):
            assert key in text

    def test_no_legacy_info_unit_schema(self):
        text = self._prompt_text()
        assert "info_units" not in text

        assert "InfoUnit" not in text
        assert "core_issue" not in text

    def test_no_legal_subject_terms(self):
        text = self._prompt_text()
        for term in ("subject_legal_type", "claim_side", "stance_target", "legal_subtype", "争议焦点"):
            assert term not in text

    def test_person_description_not_objective(self):
        text = self._prompt_text()
        assert "客观描述" not in text
        assert "明确描述" in text

    def test_source_evidence_distinction(self):
        text = self._prompt_text()
        assert "Source 与 Evidence 的区别" in text
        # 不强制每个文件都产出 Evidence
        assert "不是每个文件" in text

    def test_raw_text_provenance_rule(self):
        text = self._prompt_text()
        assert "没有 `file_id`" in text
        assert "source_refs` 留空数组 `[]`" in text


# ---------------------------------------------------------------------------
# Claim 强类型结构化字段：time / participant_refs / location_ref / object_refs
# ---------------------------------------------------------------------------


class TestClaimStructuredFields:
    def test_j_claim_describes_event_with_typed_fields(self):
        """验收 1：王强说"22:10 李明进入仓库" → 仅 Claim + 强类型字段，不生成 Event。

        该 claim 只有时刻、无日期 → `sort_value` 必须为 `null`（不得复制 label）。
        """
        out = _materialize({
            "persons": [
                {"id": "person-w", "name": "王强"},
                {"id": "person-l", "name": "李明"},
            ],
            "locations": [{"id": "location-wh", "name": "仓库"}],
            "claims": [{
                "id": "claim-1",
                "claimant_ref": "person-w",
                "proposition": "李明进入仓库",
                "time": {"label": "22:10", "sort_value": None, "precision": "exact"},
                "participant_refs": ["person-l"],
                "location_ref": "location-wh",
            }],
        })
        assert out["events"] == []  # 不生成对应 Event
        assert len(out["claims"]) == 1
        claim = out["claims"][0]
        assert claim.claimant_ref == out["persons"][0].id
        assert claim.participant_refs == [out["persons"][1].id]
        assert claim.location_ref == out["locations"][0].id
        assert claim.time is not None
        assert claim.time.label == "22:10"
        assert claim.time.sort_value is None  # 无日期 → 不可排序
        assert claim.time.precision == "exact"
        assert claim.object_refs == []

    def test_k_claimant_also_participant(self):
        """验收 2：王强说"22:10 我一直在宿舍" → claimant 本人也是 participant。"""
        out = _materialize({
            "persons": [{"id": "person-w", "name": "王强"}],
            "locations": [{"id": "location-dorm", "name": "宿舍"}],
            "claims": [{
                "id": "claim-1",
                "claimant_ref": "person-w",
                "proposition": "我一直在宿舍",
                "time": {"label": "22:10", "sort_value": None, "precision": "exact"},
                "participant_refs": ["person-w"],
                "location_ref": "location-dorm",
            }],
        })
        claim = out["claims"][0]
        assert claim.claimant_ref == out["persons"][0].id
        assert claim.participant_refs == [out["persons"][0].id]
        assert claim.location_ref == out["locations"][0].id
        assert claim.time is not None and claim.time.label == "22:10"

    def test_l_claim_without_time_is_valid(self):
        """验收 3：王强说"这把刀是李明的" → 合法 Claim，time 可为空。"""
        out = _materialize({
            "persons": [
                {"id": "person-w", "name": "王强"},
                {"id": "person-l", "name": "李明"},
            ],
            "objects": [{"id": "object-k", "name": "刀"}],
            "claims": [{
                "id": "claim-1",
                "claimant_ref": "person-w",
                "proposition": "这把刀是李明的",
                "participant_refs": ["person-l"],
                "object_refs": ["object-k"],
            }],
        })
        assert out["events"] == []
        claim = out["claims"][0]
        assert claim.time is None
        assert claim.participant_refs == [out["persons"][1].id]
        assert claim.object_refs == [out["objects"][0].id]
        assert claim.location_ref is None

    def test_typed_fields_rewritten_to_global_ids(self):
        """新增 ref 字段与 claimant_ref / about_refs 一样重写为全局 id。"""
        out = _materialize({
            "persons": [
                {"id": "person-w", "name": "王强"},
                {"id": "person-l", "name": "李明"},
            ],
            "locations": [{"id": "location-wh", "name": "仓库"}],
            "objects": [{"id": "object-k", "name": "刀"}],
            "claims": [{
                "id": "claim-1",
                "claimant_ref": "person-w",
                "proposition": "x",
                "participant_refs": ["person-l"],
                "location_ref": "location-wh",
                "object_refs": ["object-k"],
            }],
        })
        claim = out["claims"][0]
        ids = {p.name: p.id for p in out["persons"]}
        assert claim.participant_refs == [ids["李明"]]
        assert claim.location_ref == out["locations"][0].id
        assert claim.object_refs == [out["objects"][0].id]

    def test_about_refs_and_typed_fields_may_overlap(self):
        """about_refs 与强类型字段允许重叠，不做收敛。"""
        out = _materialize({
            "persons": [
                {"id": "person-w", "name": "王强"},
                {"id": "person-l", "name": "李明"},
            ],
            "claims": [{
                "id": "claim-1",
                "claimant_ref": "person-w",
                "proposition": "李明拿着刀。",
                "about_refs": ["person-l"],
                "participant_refs": ["person-l"],
            }],
        })
        claim = out["claims"][0]
        assert claim.about_refs == claim.participant_refs == [out["persons"][1].id]

    def test_typed_fields_default_empty_for_legacy_output(self):
        """未输出新字段时（LLM 旧行为），schema 默认空值，不报错。"""
        out = _materialize({
            "claims": [{"id": "claim-1", "proposition": "灯亮着"}],
        })
        claim = out["claims"][0]
        assert claim.time is None
        assert claim.participant_refs == []
        assert claim.location_ref is None
        assert claim.object_refs == []


# ---------------------------------------------------------------------------
# EventTime canonical contract（v1）
#
# `sort_value` 只在 source / context 足以可靠得到完整「年 + 月 + 日 + 时间」
# 时才生成，统一格式 `YYYY-MM-DDTHH:MM`；其余一律 `null`。
# `precision` 表示 source 时间陈述的确定程度，不表示粒度。
# ---------------------------------------------------------------------------


class TestEventTimeCanonicalContract:
    def test_1_full_datetime_canonical(self):
        """`2024年3月12日20:00` → canonical `2024-03-12T20:00`。"""
        out = _materialize({
            "events": [{
                "id": "event-1",
                "description": "2024年3月12日20:00，李明进入了仓库。",
                "time": {"label": "2024年3月12日20:00", "sort_value": "2024-03-12T20:00", "precision": "exact"},
            }],
        })
        event = out["events"][0]
        assert event.time.label == "2024年3月12日20:00"
        assert event.time.sort_value == "2024-03-12T20:00"
        assert event.time.precision == "exact"

    def test_2_month_day_without_year_is_null(self):
        """`3月12日晚8点` / `3月12日20:03` → 缺年份 → `sort_value=null`，label 保留原文。"""
        out = _materialize({
            "events": [
                {
                    "id": "event-1",
                    "description": "3月12日晚8点，李明进入了仓库。",
                    "time": {"label": "3月12日晚8点", "sort_value": None, "precision": "exact"},
                },
                {
                    "id": "event-2",
                    "description": "3月12日20:03，监控记录显示有人进入仓库。",
                    "time": {"label": "3月12日20:03", "sort_value": None, "precision": "exact"},
                },
            ],
        })
        for event in out["events"]:
            assert event.time.sort_value is None
        assert out["events"][0].time.label == "3月12日晚8点"
        assert out["events"][1].time.label == "3月12日20:03"

    def test_2b_full_date_with_minute_canonical(self):
        """有完整年月日 + 分钟 → canonical 保留分钟。"""
        out = _materialize({
            "events": [{
                "id": "event-1",
                "description": "2024年3月12日20:03，监控记录显示有人进入仓库。",
                "time": {"label": "2024年3月12日20:03", "sort_value": "2024-03-12T20:03", "precision": "exact"},
            }],
        })
        assert out["events"][0].time.sort_value == "2024-03-12T20:03"

    def test_3_time_only_is_null(self):
        """仅 `20:03`、无日期上下文 → `sort_value=null`。"""
        out = _materialize({
            "events": [{
                "id": "event-1",
                "description": "20:03 有人进入仓库。",
                "time": {"label": "20:03", "sort_value": None, "precision": "exact"},
            }],
        })
        event = out["events"][0]
        assert event.time.label == "20:03"
        assert event.time.sort_value is None

    def test_4_relative_time_is_null(self):
        """`当晚` 等相对时间无锚点 → `sort_value=null`，不猜锚点。"""
        out = _materialize({
            "persons": [{"id": "person-w", "name": "王强"}],
            "claims": [{
                "id": "claim-1",
                "claimant_ref": "person-w",
                "proposition": "我当晚看见李明进入仓库。",
                "time": {"label": "当晚", "sort_value": None, "precision": "unknown"},
            }],
        })
        claim = out["claims"][0]
        assert claim.time.label == "当晚"
        assert claim.time.sort_value is None

    def test_4b_relative_variants_all_null(self):
        """`昨晚` / `随后` / `十分钟后` 一律 `null`。"""
        out = _materialize({
            "events": [
                {"id": f"event-{i}", "description": f"事件{i}", "time": {"label": lb, "sort_value": None}}
                for i, lb in enumerate(["昨晚", "随后", "十分钟后"], start=1)
            ],
        })
        for event in out["events"]:
            assert event.time.sort_value is None

    def test_5_approximate_precision_preserved(self):
        """`大约3月12日晚8点` → `precision=approximate`，`sort_value=null`。"""
        out = _materialize({
            "events": [{
                "id": "event-1",
                "description": "大约3月12日晚8点，有人进入仓库。",
                "time": {"label": "大约3月12日晚8点", "sort_value": None, "precision": "approximate"},
            }],
        })
        event = out["events"][0]
        assert event.time.precision == "approximate"
        assert event.time.sort_value is None

    def test_5b_exact_does_not_imply_granularity(self):
        """`3月12日晚8点` 陈述明确 → `precision=exact`，但缺年份仍 `sort_value=null`。

        确定性 ≠ 粒度。
        """
        out = _materialize({
            "events": [{
                "id": "event-1",
                "description": "3月12日晚8点，李明进入了仓库。",
                "time": {"label": "3月12日晚8点", "sort_value": None, "precision": "exact"},
            }],
        })
        event = out["events"][0]
        assert event.time.precision == "exact"
        assert event.time.sort_value is None

    def test_6_event_and_claim_share_same_contract(self):
        """Event.time 与 Claim.time 使用完全相同的 EventTime contract。"""
        canonical = {"label": "2024年3月12日20:00", "sort_value": "2024-03-12T20:00", "precision": "exact"}
        out = _materialize({
            "persons": [{"id": "person-w", "name": "王强"}],
            "events": [{"id": "event-1", "description": "有人进入仓库。", "time": dict(canonical)}],
            "claims": [{"id": "claim-1", "claimant_ref": "person-w", "proposition": "有人进入仓库", "time": dict(canonical)}],
        })
        event_time = out["events"][0].time
        claim_time = out["claims"][0].time
        assert isinstance(event_time, EventTime) and isinstance(claim_time, EventTime)
        assert event_time.model_dump() == claim_time.model_dump()
        assert set(event_time.model_dump().keys()) == {"label", "sort_value", "precision"}

    def test_7_natural_language_sort_value_is_removed(self):
        """若 LLM 误把自然语言写进 `sort_value`，materialize 后仍在字段中，
        但在 Timeline runtime 会被降级（见 test_timeline）。此处只固化字段可见性。
        """
        out = _materialize({
            "events": [{
                "id": "event-1",
                "description": "3月12日晚8点，李明进入了仓库。",
                "time": {"label": "3月12日晚8点", "sort_value": "3月12日晚8点", "precision": "exact"},
            }],
        })
        # 字段原样保留（不静默改写 extraction 产物）
        assert out["events"][0].time.sort_value == "3月12日晚8点"
