"""真实 LLM 环境的 E2E smoke test（Phase 2）。

验证完整链路：fixture 上传 → parse → extraction（typed）→ relation（semantic）→ reasoning（disabled）。
只检查 semantic invariants，不要求 LLM 字面输出一致。

默认 skip（真实 LLM 耗时 + 消耗 token）。启用方式：
    RUN_LLM_SMOKE=1 .venv/Scripts/python.exe -m pytest tests/test_llm_smoke.py -v
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.case_service as case_service_module
from app.main import app

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_LLM_SMOKE") != "1",
    reason="需要真实 LLM 环境，设置 RUN_LLM_SMOKE=1 才运行",
)

FIXTURES = Path(__file__).parent / "fixtures"
WAREHOUSE = FIXTURES / "warehouse_conflict.txt"
CLAIM_KNIFE = FIXTURES / "claim_knife.txt"
CCTV_MENTION = FIXTURES / "cctv_mention.txt"
IDENTITY_GUESS = FIXTURES / "identity_guess.txt"
DIALOGUE_ONLY = FIXTURES / "dialogue_only.txt"
CLAIM_TIME_ENTER = FIXTURES / "claim_time_enter_warehouse.txt"
CLAIM_SELF_ALIBI = FIXTURES / "claim_self_alibi.txt"
CLAIM_KNIFE_OWNER = FIXTURES / "claim_knife_owner.txt"

client = TestClient(app)


def _upload(path: Path, title: str) -> dict:
    content = path.read_bytes()
    resp = client.post(
        "/api/cases",
        data={"case_title": title},
        files={"files": (path.name, content, "text/plain")},
    )
    assert resp.status_code == 200, f"上传失败: {resp.text[:500]}"
    return resp.json()["case"]


def _all_entities(case: dict) -> list[dict]:
    entities: list[dict] = []
    for key in ("persons", "events", "locations", "objects", "claims", "evidences"):
        entities.extend(case.get(key, []) or [])
    return entities


def _entity_text(e: dict) -> str:
    return str(e.get("name") or e.get("proposition") or e.get("description") or "")


def _persons_by_id(case: dict) -> dict[str, str]:
    return {p["id"]: p["name"] for p in case.get("persons", []) or []}


# ---------------------------------------------------------------------------
# Extraction：Phase 2 semantic invariants
# ---------------------------------------------------------------------------


def test_extraction_phase2_contract():
    case = _upload(WAREHOUSE, "llm-smoke-仓库冲突")

    entities = _all_entities(case)
    assert len(entities) > 0, "extraction 应产生非空 entities"

    texts = " ".join(_entity_text(e) for e in entities)

    # 识别出人物"李明"与"仓库"
    assert "李明" in texts, f"应识别出李明，实际={texts}"
    assert "仓库" in texts, f"应识别出仓库相关实体，实际={texts}"

    # 1. narrator → Event（旁白"李明进入仓库"至少产出一个 Event）
    assert any(
        "仓库" in (e.get("description") or "")
        for e in case.get("events", [])
    ), "旁白应被抽取为 Event"

    # 2. character statement → Claim（王强 / 李明的口述至少两条 Claim）
    assert len(case.get("claims", [])) >= 2, "王强 / 李明的口述应被抽取为 Claim"

    # 3. CCTV 不确定身份：保留限定"相似"，且不错误绑定为"李明"
    assert "相似" in texts, "CCTV 的不确定限定'相似'应被保留"
    persons = _persons_by_id(case)
    for ev in case.get("events", []):
        if "相似" in (ev.get("description") or ""):
            bound_names = [
                persons[r] for r in ev.get("participant_refs", []) if r in persons
            ]
            assert "李明" not in bound_names, "CCTV 相似者不得被直接绑定为李明"

    # 5. 输出完全符合新 schema（无 legacy info_units）
    assert "info_units" not in case
    assert "core_issue" not in case
    for key in ("persons", "events", "locations", "objects", "claims", "evidences"):
        assert key in case

    # 6. legacy adapter 已删除
    assert not hasattr(case_service_module, "_normalize_extracted_unit")


def test_extraction_claim_entities_not_upgraded():
    case = _upload(CLAIM_KNIFE, "llm-smoke-李明拿刀")

    # 4. Claim 中的实体仍抽取：王强 / 李明 / 刀 + Claim
    names = {p["name"] for p in case.get("persons", []) or []}
    assert "王强" in names, f"应抽取人物王强，实际={names}"
    assert "李明" in names, f"应抽取人物李明，实际={names}"

    obj_texts = " ".join(
        str(o.get("name") or o.get("description") or "")
        for o in case.get("objects", []) or []
    )
    assert "刀" in obj_texts, f"应抽取物件'刀'，实际={obj_texts}"

    assert len(case.get("claims", [])) >= 1, "应产出一条 Claim（王强说李明拿刀）"

    # 不升级为世界 Event / ownership：纯角色口述不产生 Event
    assert case.get("events", []) == [], "Claim 中的'拿刀'不得升级为 Event"


# ---------------------------------------------------------------------------
# Relation / Reasoning：保持 Phase 1c 行为
# ---------------------------------------------------------------------------


def test_relation_produces_contradicts():
    case = _upload(WAREHOUSE, "llm-smoke-仓库冲突")

    resp = client.post(
        "/api/relation/run",
        json={"case_id": case["case_id"]},
    )
    assert resp.status_code == 200, f"relation 失败: {resp.text[:500]}"
    data = resp.json()

    relations = data["semantic_relations"]
    assert len(relations) > 0, "relation 应产生非空语义关系"

    relation_types = [r["relation_type"] for r in relations]
    assert "contradicts" in relation_types, (
        f"应识别出 contradicts 冲突关系，实际 types={relation_types}"
    )

    edge_types = [e["edge_type"] for e in data["new_edges"]]
    assert "contradicts" in edge_types, f"contradicts 应投影为 contradicts 边，实际={edge_types}"


def test_reasoning_disabled_returns_501():
    case = _upload(WAREHOUSE, "llm-smoke-仓库冲突")

    resp = client.post(
        "/api/reasoning/run",
        json={
            "case_id": case["case_id"],
            "selected_info_units": [],
            "selected_relations": [],
            "user_prompt": "",
        },
    )
    assert resp.status_code == 501, f"reasoning 应被显式禁用（501），实际={resp.status_code}"
    body = resp.json()
    assert body["detail"]["status"] == "disabled"
    assert body["detail"]["stage"] == "reasoning"


# ---------------------------------------------------------------------------
# Prompt contract real LLM smoke（Phase 2 contract correction）
# ---------------------------------------------------------------------------


def test_cctv_keyword_not_auto_event():
    """角色口述中仅出现"监控"关键词，不得自动升级为世界 Event。"""
    case = _upload(CCTV_MENTION, "llm-smoke-监控转述")

    claims = case.get("claims", []) or []
    claim_texts = " ".join(str(c.get("proposition") or "") for c in claims)
    assert "监控" in claim_texts, f"应保留角色口述 Claim，实际={claim_texts}"

    # source 是角色口述，非直接记录 → 不生成 Event
    assert case.get("events", []) == [], (
        "角色口述中出现'监控'关键词不得自动升级为世界 Event"
    )

    # 不机械生成源文件 Evidence
    _assert_no_filename_evidence(case, "cctv_mention.txt")


def test_source_identity_guess_kept_as_claim():
    """source 明示"黑衣人可能是老陈"→ 保留为 Claim，不 merge Person，不生成 same_as。"""
    case = _upload(IDENTITY_GUESS, "llm-smoke-身份猜测")

    claims = case.get("claims", []) or []
    claim_texts = " ".join(str(c.get("proposition") or "") for c in claims)
    assert "黑衣" in claim_texts or "老陈" in claim_texts, (
        f"source 明示的身份猜测应保留为 Claim，实际={claim_texts}"
    )

    # 不 merge Person：不得把"黑衣人"与"老陈"归一为同一个人
    for p in case.get("persons", []) or []:
        name = str(p.get("name") or "")
        aliases = [str(a) for a in (p.get("aliases") or [])]
        has_laochen = any("老陈" in t for t in [name, *aliases])
        has_hei = any("黑衣" in t for t in [name, *aliases])
        assert not (has_laochen and has_hei), (
            f"不得把'黑衣人'与'老陈'merge 为同一 Person：name={name}, aliases={aliases}"
        )

    # 不生成 same_as 字段
    for e in _all_entities(case):
        assert "same_as" not in e, "extraction 不得生成 same_as 字段"

    # 不机械生成源文件 Evidence
    _assert_no_filename_evidence(case, "identity_guess.txt")


def test_dialogue_source_no_mechanical_evidence():
    """纯角色对话 source 不应机械生成 Evidence'角色剧本文件'。"""
    case = _upload(DIALOGUE_ONLY, "llm-smoke-纯角色对话")

    # 角色陈述应抽取为 Claim
    assert len(case.get("claims", []) or []) >= 1, "角色对话应被抽取为 Claim"

    # 纯角色对话 source 不应机械生成 Evidence
    assert case.get("evidences", []) == [], (
        f"纯角色对话 source 不应机械生成 Evidence，实际={case.get('evidences')}"
    )


def _assert_no_filename_evidence(case: dict, filename: str):
    """输入文件名只是承载容器，不得被机械生成为 Evidence。"""
    for e in case.get("evidences", []) or []:
        desc = str(e.get("description") or "")
        assert filename not in desc and ".txt" not in desc, (
            f"输入文件名不得被机械生成为 Evidence：{desc}"
        )


# ---------------------------------------------------------------------------
# Claim 强类型结构化字段 real LLM smoke（验收 1 / 2 / 3）
# ---------------------------------------------------------------------------


def _persons_by_name(case: dict) -> dict[str, str]:
    return {p["name"]: p["id"] for p in case.get("persons", []) or []}


def test_claim_structured_time_participant_location_and_no_event():
    """验收 1：王强说"22:10 李明进入仓库" → 只生成 Claim（含强类型字段），不生成对应 Event。"""
    case = _upload(CLAIM_TIME_ENTER, "llm-smoke-22:10进入仓库")

    claims = case.get("claims", []) or []
    assert len(claims) >= 1, "角色口述应被抽取为 Claim"

    names = _persons_by_name(case)
    assert "王强" in names, f"应抽取 claimant 王强，实际={names}"
    assert "李明" in names, f"应抽取 participant 李明，实际={names}"

    loc_texts = " ".join(str(loc.get("name") or loc.get("description") or "")
                         for loc in case.get("locations", []) or [])
    assert "仓库" in loc_texts, f"应抽取地点仓库，实际={loc_texts}"

    # 找到王强那条 Claim
    claim = next((c for c in claims if c.get("claimant_ref") == names["王强"]), None)
    assert claim is not None, f"应存在 claimant=王强 的 Claim，实际={claims}"

    assert claim.get("participant_refs"), "Claim 应结构化记录 participant（李明）"
    assert names["李明"] in claim["participant_refs"], (
        f"participant_refs 应含李明，实际={claim.get('participant_refs')}"
    )
    assert claim.get("time"), "Claim 应记录 time（22:10）"
    assert "22:10" in str(claim["time"].get("label") or ""), (
        f"time.label 应保留原文 22:10，实际={claim.get('time')}"
    )
    # canonical contract v1：只有时刻、无日期 → 不可排序
    assert claim["time"].get("sort_value") is None, (
        f"无日期上下文时应为 sort_value=null（不得复制 label），实际={claim.get('time')}"
    )
    assert claim.get("location_ref"), "Claim 应记录 location_ref（仓库）"

    # 不生成该陈述对应的世界 Event
    assert case.get("events", []) == [], (
        f"角色陈述不得升级为世界 Event，实际={case.get('events')}"
    )


def test_claim_self_participant_and_alibi():
    """验收 2：王强说"22:10 我一直在宿舍" → claimant 本人也是 proposition participant。"""
    case = _upload(CLAIM_SELF_ALIBI, "llm-smoke-自我不在场")

    claims = case.get("claims", []) or []
    assert len(claims) >= 1, "角色口述应被抽取为 Claim"

    names = _persons_by_name(case)
    assert "王强" in names, f"应抽取 claimant 王强，实际={names}"

    claim = next((c for c in claims if c.get("claimant_ref") == names["王强"]), None)
    assert claim is not None, f"应存在 claimant=王强 的 Claim，实际={claims}"

    # claimant 本人也是 participant
    assert names["王强"] in (claim.get("participant_refs") or []), (
        f"claimant 本人应出现在 participant_refs，实际={claim.get('participant_refs')}"
    )
    assert claim.get("time"), "Claim 应记录 time（22:10）"
    assert claim.get("location_ref"), "Claim 应记录 location_ref（宿舍）"

    loc_texts = " ".join(str(loc.get("name") or loc.get("description") or "")
                         for loc in case.get("locations", []) or [])
    assert "宿舍" in loc_texts, f"应抽取地点宿舍，实际={loc_texts}"

    assert case.get("events", []) == [], "角色陈述不得升级为世界 Event"


def test_claim_ownership_object_without_time():
    """验收 3：王强说"这把刀是李明的" → 合法 Claim，object/participant 结构化，time 可为空。"""
    case = _upload(CLAIM_KNIFE_OWNER, "llm-smoke-刀归属")

    claims = case.get("claims", []) or []
    assert len(claims) >= 1, "角色口述应被抽取为 Claim"

    names = _persons_by_name(case)
    assert "王强" in names, f"应抽取 claimant 王强，实际={names}"
    assert "李明" in names, f"应抽取 participant 李明，实际={names}"

    obj_texts = " ".join(str(o.get("name") or o.get("description") or "")
                         for o in case.get("objects", []) or [])
    assert "刀" in obj_texts, f"应抽取物件刀，实际={obj_texts}"

    claim = next((c for c in claims if c.get("claimant_ref") == names["王强"]), None)
    assert claim is not None, f"应存在 claimant=王强 的 Claim，实际={claims}"

    assert claim.get("participant_refs"), "Claim 应结构化记录涉及的人（李明）"
    assert claim.get("object_refs"), "Claim 应结构化记录涉及的物体（刀）"

    # 不产生 ownership / 持刀 Event
    assert case.get("events", []) == [], (
        f"Claim 中的物件归属不得升级为所有权 / Event，实际={case.get('events')}"
    )

