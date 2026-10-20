"""Phase 3 — Timeline runtime projection 测试。

覆盖：
1. Event 按时间排序。
2. Event + Claim 混排。
3. Claim 自己的 time 被正确使用。
4. time=None 不进入 Timeline。
5. approximate time 可进入。
6. 相同时间排序稳定（deterministic tie-break）。
7. Timeline 不修改 CaseData。
8. Timeline 路径不调用 LLM。
9. API contract：GET /api/cases/{case_id}/timeline 返回 items。
10. sort_value 缺失的 item 稳定排在可排序时间之后。
11. legacy / 非 canonical sort_value 在 runtime 降级为不可排序（不重写历史数据）。
12. canonical `YYYY-MM-DDTHH:MM` 之间按真实时间先后排序。

Timeline 是 runtime projection：不持久化、不建事实实体、不经过 inference pipeline。
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from fastapi.testclient import TestClient

import app.case_service as case_service_module
from app.case_service import build_timeline
from app.main import app
from app.schemas import (
    CaseData,
    Claim,
    Event,
    EventTime,
    Location,
    Object,
    Person,
    TimelineItemType,
)

NOW = datetime(2024, 1, 1)
client = TestClient(app)


def _empty_case() -> CaseData:
    return CaseData(case_id="case-tl", created_at=NOW, updated_at=NOW)


def _event(id_: str, label: str, sort_value: str | None = "2024-03-12T20:00", **kwargs) -> Event:
    return Event(
        id=id_,
        description=f"事件 {id_}",
        time=EventTime(label=label, sort_value=sort_value, precision=kwargs.pop("precision", "exact")),
        created_at=NOW,
        updated_at=NOW,
        source_refs=[],
        confidence="medium",
        evidence_quote="",
        **kwargs,
    )


def _claim(id_: str, label: str, sort_value: str | None = "2024-03-12T22:10", **kwargs) -> Claim:
    return Claim(
        id=id_,
        proposition=f"陈述 {id_}",
        time=EventTime(label=label, sort_value=sort_value, precision=kwargs.pop("precision", "exact")),
        created_at=NOW,
        updated_at=NOW,
        source_refs=[],
        confidence="medium",
        evidence_quote="",
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 1–6：投影与排序
# ---------------------------------------------------------------------------


class TestBuildTimelineOrdering:
    def test_1_events_sorted_by_time(self):
        c = _empty_case()
        c.events.append(_event("event-b", "20:00", "2024-03-12T20:00"))
        c.events.append(_event("event-a", "18:00", "2024-03-12T18:00"))
        tl = build_timeline(c)
        assert [i.item_ref for i in tl.items] == ["event-a", "event-b"]

    def test_2_event_and_claim_mixed(self):
        c = _empty_case()
        c.events.append(_event("event-a", "18:00", "2024-03-12T18:00"))
        c.claims.append(_claim("claim-a", "19:00", "2024-03-12T19:00"))
        c.events.append(_event("event-b", "21:00", "2024-03-12T21:00"))
        tl = build_timeline(c)
        assert [(i.item_type, i.item_ref) for i in tl.items] == [
            (TimelineItemType.event, "event-a"),
            (TimelineItemType.claim, "claim-a"),
            (TimelineItemType.event, "event-b"),
        ]

    def test_3_claim_own_time_used(self):
        c = _empty_case()
        c.claims.append(_claim("claim-a", "22:10", "2024-03-12T22:10"))
        tl = build_timeline(c)
        assert len(tl.items) == 1
        assert tl.items[0].item_type == TimelineItemType.claim
        assert tl.items[0].item_ref == "claim-a"
        assert tl.items[0].time.sort_value == "2024-03-12T22:10"

    def test_4_time_none_excluded(self):
        c = _empty_case()
        c.events.append(Event(id="event-no-time", description="x", time=None,
                              created_at=NOW, updated_at=NOW, source_refs=[], confidence="medium", evidence_quote=""))
        c.claims.append(Claim(id="claim-no-time", proposition="y", time=None,
                              created_at=NOW, updated_at=NOW, source_refs=[], confidence="medium", evidence_quote=""))
        c.events.append(_event("event-a", "20:00"))
        tl = build_timeline(c)
        refs = [i.item_ref for i in tl.items]
        assert "event-no-time" not in refs
        assert "claim-no-time" not in refs
        assert refs == ["event-a"]

    def test_5_approximate_time_included(self):
        c = _empty_case()
        c.events.append(_event("event-approx", "大约三月", "2024-03", precision="approximate"))
        tl = build_timeline(c)
        assert len(tl.items) == 1
        assert tl.items[0].time.precision == "approximate"

    def test_6_same_sort_value_stable_tiebreak(self):
        c = _empty_case()
        # 相同 sort_value：tie-break 由 item_type + item_ref 决定，顺序确定。
        # item_type 字典序 "claim" < "event"，故同时间点 claim 先于 event。
        c.events.append(_event("event-b", "20:00", "2024-03-12T20:00"))
        c.events.append(_event("event-a", "20:00", "2024-03-12T20:00"))
        c.claims.append(_claim("claim-a", "20:00", "2024-03-12T20:00"))
        tl = build_timeline(c)
        assert [(i.item_type, i.item_ref) for i in tl.items] == [
            (TimelineItemType.claim, "claim-a"),
            (TimelineItemType.event, "event-a"),
            (TimelineItemType.event, "event-b"),
        ]
        # 多次运行结果一致（deterministic）。
        assert [i.item_ref for i in build_timeline(c).items] == [i.item_ref for i in tl.items]

    def test_10_missing_sort_value_sorted_last(self):
        """time 存在但 sort_value 不可用 → 稳定排在可排序时间之后。"""
        c = _empty_case()
        c.events.append(_event("event-nosort", "时间不明", None))
        c.claims.append(_claim("claim-a", "18:00", "2024-03-12T18:00"))
        tl = build_timeline(c)
        assert [i.item_ref for i in tl.items] == ["claim-a", "event-nosort"]
        # 两个都没有 sort_value 时，仍按 type+ref 稳定排序，且都在有 sort 的后面。
        c.events.append(_event("event-nosort-b", "时间不明", None))
        tl2 = build_timeline(c)
        assert [i.item_ref for i in tl2.items] == ["claim-a", "event-nosort", "event-nosort-b"]


# ---------------------------------------------------------------------------
# 7–8：无副作用 / 无 LLM
# ---------------------------------------------------------------------------


class TestTimelineSideEffects:
    def test_7_does_not_mutate_case(self):
        c = _empty_case()
        c.events.append(_event("event-b", "20:00", "2024-03-12T20:00"))
        c.events.append(_event("event-a", "18:00", "2024-03-12T18:00"))
        before_events = [e.model_dump() for e in c.events]
        before_title = c.case_title
        build_timeline(c)
        assert [e.model_dump() for e in c.events] == before_events
        assert c.case_title == before_title
        # CaseData 不含 timeline 字段（Timeline 不持久化）。
        assert "timeline" not in c.model_dump()

    def test_8_does_not_call_llm(self, monkeypatch):
        called = {"n": 0}

        def _boom(*a, **k):
            called["n"] += 1
            raise AssertionError("Timeline 不应调用 LLM")

        monkeypatch.setattr(case_service_module, "_call_llm_sync", _boom)
        c = _empty_case()
        c.events.append(_event("event-a", "18:00"))
        build_timeline(c)
        assert called["n"] == 0


# ---------------------------------------------------------------------------
# 9：API contract
# ---------------------------------------------------------------------------


def _patch_no_disk(monkeypatch):
    monkeypatch.setattr("app.main.save_case", lambda *a, **k: None)
    monkeypatch.setattr("app.case_store.save_case", lambda *a, **k: None)


class TestTimelineApiContract:
    def test_9_get_timeline_returns_items(self, monkeypatch):
        case = _empty_case()
        case.persons.append(Person(id="person-w", name="王强", created_at=NOW, updated_at=NOW,
                                   source_refs=[], confidence="medium", evidence_quote=""))
        case.events.append(_event("event-a", "18:00", "2024-03-12T18:00"))
        case.claims.append(_claim("claim-a", "22:10", "2024-03-12T22:10", claimant_ref="person-w"))

        monkeypatch.setattr("app.main.load_case", lambda case_id: case)

        resp = client.get("/api/cases/case-tl/timeline")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["success"] is True
        items = body["timeline"]["items"]
        assert [i["item_ref"] for i in items] == ["event-a", "claim-a"]
        assert [i["item_type"] for i in items] == ["event", "claim"]
        assert body["timeline"]["case_id"] == "case-tl"

    def test_9b_missing_case_returns_404(self, monkeypatch):
        monkeypatch.setattr("app.main.load_case", lambda case_id: None)
        resp = client.get("/api/cases/nope/timeline")
        assert resp.status_code == 404

    def test_9c_timeline_route_does_not_touch_inference_pipeline(self):
        """Timeline 走独立只读 endpoint，不经 disabled 的 inference pipeline。"""
        paths = {(r.path, tuple(sorted(getattr(r, "methods", []) or []))) for r in app.routes}
        assert ("/api/cases/{case_id}/timeline", ("GET",)) in paths


# ---------------------------------------------------------------------------
# 11–14：legacy / 非 canonical sort_value 在 runtime 降级
#
# 历史数据不批量重写。Timeline 排序侧做格式识别：只有严格 canonical
# `YYYY-MM-DDTHH:MM` 参与排序，其余（legacy 自然语言、partial key）等价于
# `sort_value=None`，稳定排在可排序项之后。
# ---------------------------------------------------------------------------


class TestLegacySortValueDowngrade:
    def _case_with_times(self, pairs: list[tuple[str, str | None]]) -> CaseData:
        case = _empty_case()
        case.events = [_event(f"event-{i}", lb, sv) for i, (lb, sv) in enumerate(pairs, start=1)]
        return case

    def test_11_natural_language_sort_value_not_sortable(self):
        """`3月12日晚8点` 不是 canonical → 等价 None，不参与可排序区。"""
        case = self._case_with_times([
            ("2024年3月12日20:00", "2024-03-12T20:00"),   # canonical
            ("3月12日晚8点", "3月12日晚8点"),              # legacy 自然语言
        ])
        tl = build_timeline(case)
        # canonical 的排前，legacy 的排后
        assert tl.items[0].item_ref == "event-1"
        assert tl.items[1].item_ref == "event-2"
        assert tl.items[0].time.sort_value == "2024-03-12T20:00"

    def test_12_time_only_sort_value_downgraded(self):
        """`20:03`（仅时刻）不是 canonical → 排到 canonical 之后。"""
        case = self._case_with_times([
            ("20:03", "20:03"),
            ("2024年3月12日22:10", "2024-03-12T22:10"),
        ])
        tl = build_timeline(case)
        assert [i.item_ref for i in tl.items] == ["event-2", "event-1"]

    def test_13_relative_time_sort_value_downgraded(self):
        """`当晚` 不是 canonical → 排到可排序项之后。"""
        case = self._case_with_times([
            ("当晚", "当晚"),
            ("2024年3月12日20:00", "2024-03-12T20:00"),
        ])
        tl = build_timeline(case)
        assert [i.item_ref for i in tl.items] == ["event-2", "event-1"]

    def test_14_all_legacy_still_deterministic(self):
        """全部为非 canonical 时，仍按 item_type + item_ref 稳定排序。"""
        case = self._case_with_times([
            ("当晚", "当晚"),
            ("20:03", "20:03"),
            ("3月12日晚8点", "3月12日晚8点"),
        ])
        tl1 = build_timeline(case)
        tl2 = build_timeline(case)
        assert [i.item_ref for i in tl1.items] == [i.item_ref for i in tl2.items] == ["event-1", "event-2", "event-3"]

    def test_14b_legacy_and_null_share_same_bucket(self):
        """legacy 非 canonical 与 `null` 落在同一「不可排序」桶，靠 tie-break 定序。"""
        case = _empty_case()
        case.events = [
            _event("event-a", "当晚", "当晚"),
            _event("event-b", "无时间", None),
            _event("event-c", "2024年3月12日20:00", "2024-03-12T20:00"),
        ]
        tl = build_timeline(case)
        assert tl.items[0].item_ref == "event-c"           # canonical 唯一排前
        assert {i.item_ref for i in tl.items[1:]} == {"event-a", "event-b"}

    def test_15_canonical_helper_rejects_partial_keys(self):
        """`canonical_sort_value` 只接受 `YYYY-MM-DDTHH:MM`，拒绝 partial key。"""
        from app.case_service import canonical_sort_value

        assert canonical_sort_value("2024-03-12T20:00") == "2024-03-12T20:00"
        for bad in ("2024-03-12T20:00:00", "2024-03-12", "20:00", "03-12T20:00",
                    "3月12日晚8点", "当晚", "", None):
            assert canonical_sort_value(bad) is None, bad

    def test_16_two_canonical_events_sorted_chronologically(self):
        """两个 canonical 事件按真实时间先后排序。"""
        case = self._case_with_times([
            ("2024年3月12日22:10", "2024-03-12T22:10"),
            ("2024年3月12日20:00", "2024-03-12T20:00"),
        ])
        tl = build_timeline(case)
        assert [i.item_ref for i in tl.items] == ["event-2", "event-1"]
