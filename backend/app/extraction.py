"""Phase 2 — Extraction typed 解析与物化（纯逻辑，不依赖 case_service / LLM）。

职责：
- 把 Extraction LLM 的原始 JSON 严格解析为 typed `ExtractionLLMResult`。
- 把 LLM 的局部 id 重映射为全局唯一 id，并重写内部引用。
- 明确拒绝 legacy InfoUnit schema（info_units / core_issue / issue_set）。

Source ≠ Reality：本模块只做「source 表达什么」的结构化落盘，不做身份合并、
same_as、truth probability、supports/contradicts 等任何推理。
"""

from __future__ import annotations

import logging
from uuid import uuid4

from pydantic import ValidationError

from app.schemas import ExtractionLLMResult

logger = logging.getLogger(__name__)


class ExtractionStageError(Exception):
    """Extraction 执行失败（error 态，Phase 2）。

    与 success 严格区分：
    - success：正常执行；entities 允许为空（正常执行但 source 无可抽取实体）。
    - error：LLM/provider/parser/reference failure。禁止静默 fallback 到 []。
    """

    def __init__(self, stage: str = "extraction", phase: str = "phase2", reason: str = "") -> None:
        self.stage = stage
        self.phase = phase
        self.reason = reason or "Extraction stage failed."
        super().__init__(self.reason)


_LEGACY_KEYS = ("info_units", "core_issue", "issue_set")


def parse_extraction_output(parsed: object) -> ExtractionLLMResult:
    """把 LLM 原始 JSON 严格解析为 typed ExtractionLLMResult。

    - 非 dict（非 JSON / 空）→ ExtractionStageError。
    - 携带 legacy InfoUnit 字段 → 显式拒绝（不做兼容适配）。
    - 不符合新 schema（字段类型错 / 携带未知字段）→ ExtractionStageError。
    """
    if not isinstance(parsed, dict):
        raise ExtractionStageError(reason="LLM 未返回 JSON 对象，无法解析为 extraction 输出")

    legacy_keys = [key for key in _LEGACY_KEYS if key in parsed]
    if legacy_keys:
        raise ExtractionStageError(
            reason=f"LLM 仍输出 legacy InfoUnit schema，已拒绝（含字段 {legacy_keys}）"
        )

    try:
        return ExtractionLLMResult.model_validate(parsed)
    except ValidationError as exc:
        raise ExtractionStageError(reason=f"extraction 输出不符合新 schema: {exc}") from exc


def materialize_extraction_output(output: ExtractionLLMResult, file_ids: list[str]) -> dict[str, list]:
    """把 typed LLM 输出物化为 6 类 entity 列表，并保证全局 id 唯一。

    - LLM 的 id 仅作批次内局部引用 key；server 重映射为 `{kind}-{hex}` 全局 id，
      避免跨批次 id 冲突（append 场景）。
    - 引用字段（claimant_ref / participant_refs / location_ref / object_refs /
      about_refs）按局部 id → 全局 id 重写。
    - source_refs 为空时默认绑定到本次抽取的 file_ids。
    - created_at / updated_at 沿用各 entity 的 default_factory（parse 时已补写）。
    """
    remap: dict[str, str] = {}

    def _gid(kind: str, local_id: str) -> str:
        key = (local_id or "").strip()
        if not key:
            return f"{kind}-{uuid4().hex[:10]}"
        if key not in remap:
            remap[key] = f"{kind}-{uuid4().hex[:10]}"
        return remap[key]

    def _sources(src: list[str]) -> list[str]:
        return src or list(file_ids)

    persons = [
        p.model_copy(update={"id": _gid("person", p.id), "source_refs": _sources(p.source_refs)})
        for p in output.persons
    ]
    locations = [
        loc.model_copy(update={"id": _gid("location", loc.id), "source_refs": _sources(loc.source_refs)})
        for loc in output.locations
    ]
    objects = [
        obj.model_copy(update={"id": _gid("object", obj.id), "source_refs": _sources(obj.source_refs)})
        for obj in output.objects
    ]
    events = [
        e.model_copy(update={"id": _gid("event", e.id), "source_refs": _sources(e.source_refs)})
        for e in output.events
    ]
    claims = [
        c.model_copy(update={"id": _gid("claim", c.id), "source_refs": _sources(c.source_refs)})
        for c in output.claims
    ]
    evidences = [
        ev.model_copy(update={"id": _gid("evidence", ev.id), "source_refs": _sources(ev.source_refs)})
        for ev in output.evidences
    ]

    def _r(ref: str) -> str:
        return remap.get(ref, ref)

    for e in events:
        e.participant_refs = [_r(r) for r in e.participant_refs]
        e.location_ref = _r(e.location_ref) if e.location_ref else None
        e.object_refs = [_r(r) for r in e.object_refs]
    for c in claims:
        c.claimant_ref = _r(c.claimant_ref) if c.claimant_ref else None
        c.about_refs = [_r(r) for r in c.about_refs]
        c.participant_refs = [_r(r) for r in c.participant_refs]
        c.location_ref = _r(c.location_ref) if c.location_ref else None
        c.object_refs = [_r(r) for r in c.object_refs]
    for ev in evidences:
        ev.about_refs = [_r(r) for r in ev.about_refs]

    return {
        "persons": persons,
        "events": events,
        "locations": locations,
        "objects": objects,
        "claims": claims,
        "evidences": evidences,
    }
