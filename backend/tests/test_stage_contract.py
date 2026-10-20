"""Phase 1c 阶段状态 contract 测试。

覆盖：
1. Relation 三态（disabled / success / error）：
   - success：正常执行，semantic_relations 允许为 []（正常执行但没找到）。
   - error：LLM/provider/parser failure → RelationStageError，绝不 fallback 到 []。
2. Reasoning 显式 disabled：
   - run_reasoning_agent 抛 ReasoningStageDisabledError；endpoint 返回 501。
3. Extraction 当前状态 = runtime-compatible through temporary legacy-output adapter；
   new extraction semantic contract not yet functionally verified。
"""

from __future__ import annotations

import asyncio
import inspect
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.case_service import (
    LLMRequestError,
    ReasoningStageDisabledError,
    RelationStageError,
    run_extraction_agent,
    run_relation_agent,
    run_reasoning_agent,
)
from app.main import app
from app.schemas import (
    CaseData,
    Claim,
    Person,
    ReasoningRequest,
    RelationRequest,
)

NOW = datetime(2024, 1, 1)


def _empty_case() -> CaseData:
    return CaseData(case_id="case-1", created_at=NOW, updated_at=NOW)


def _case_with_claims() -> CaseData:
    case = _empty_case()
    case.persons.append(Person(id="person-w", name="王", created_at=NOW, updated_at=NOW, source_refs=[], confidence="medium", evidence_quote=""))
    case.claims.append(Claim(id="claim-1", proposition="灯亮着", claimant_ref="person-w", source_refs=[], confidence="high", evidence_quote="", created_at=NOW, updated_at=NOW))
    case.claims.append(Claim(id="claim-2", proposition="灯没亮", claimant_ref="person-w", source_refs=[], confidence="high", evidence_quote="", created_at=NOW, updated_at=NOW))
    return case


def _patch_no_disk(monkeypatch):
    monkeypatch.setattr("app.case_service.append_case_log", lambda *a, **k: None)


def _llm_result(parsed, raw_reply="ok"):
    return {
        "parsed_json": parsed,
        "raw_reply": raw_reply,
        "request_meta": {},
        "response_meta": {"http_status": 200},
    }


# ---------------------------------------------------------------------------
# Relation 三态：success / error
# ---------------------------------------------------------------------------


class TestRelationSuccessState:
    def test_success_with_relations_persists(self, monkeypatch):
        _patch_no_disk(monkeypatch)
        monkeypatch.setattr(
            "app.case_service._call_llm_sync",
            lambda sp, up: _llm_result({"semantic_relations": [
                {"source_id": "claim-1", "target_id": "claim-2", "relation_type": "contradicts",
                 "detection_confidence": "high", "rationale": ""},
            ]}),
        )
        case = _case_with_claims()
        req = RelationRequest(case_id="case-1")

        relations, edges = asyncio.run(run_relation_agent(case, req))

        assert len(relations) == 1
        assert relations[0].relation_type.value == "contradicts"
        # persisted
        assert len(case.semantic_relations) == 1
        # semantic projection：contradicts → GraphEdgeType.contradicts
        assert edges and edges[0].edge_type.value == "contradicts"

    def test_success_with_empty_relations_is_valid(self, monkeypatch):
        """success + semantic_relations=[] 代表「正常执行但没找到关系」，不是 error。"""
        _patch_no_disk(monkeypatch)
        monkeypatch.setattr(
            "app.case_service._call_llm_sync",
            lambda sp, up: _llm_result({"semantic_relations": []}),
        )
        case = _case_with_claims()
        req = RelationRequest(case_id="case-1")

        relations, edges = asyncio.run(run_relation_agent(case, req))

        assert relations == []
        assert edges == []


class TestRelationErrorState:
    def test_parser_failure_raises_error_not_empty(self, monkeypatch):
        """LLM 返回非 JSON / 无 semantic_relations list → error，不 fallback 到 []。"""
        _patch_no_disk(monkeypatch)
        monkeypatch.setattr(
            "app.case_service._call_llm_sync",
            lambda sp, up: _llm_result({}, raw_reply="not json at all"),
        )
        case = _case_with_claims()
        req = RelationRequest(case_id="case-1")

        with pytest.raises(RelationStageError):
            asyncio.run(run_relation_agent(case, req))

    def test_provider_failure_raises_error(self, monkeypatch):
        _patch_no_disk(monkeypatch)

        def _boom(sp, up):
            raise LLMRequestError("provider down", status_code=500)

        monkeypatch.setattr("app.case_service._call_llm_sync", _boom)
        case = _case_with_claims()
        req = RelationRequest(case_id="case-1")

        with pytest.raises(RelationStageError):
            asyncio.run(run_relation_agent(case, req))


# ---------------------------------------------------------------------------
# Reasoning：显式 disabled
# ---------------------------------------------------------------------------


class TestReasoningStageDisabled:
    def test_run_reasoning_agent_raises(self, monkeypatch):
        _patch_no_disk(monkeypatch)
        case = _empty_case()
        req = ReasoningRequest(case_id="case-1")

        with pytest.raises(ReasoningStageDisabledError):
            asyncio.run(run_reasoning_agent(case, req))

    def test_reasoning_endpoint_returns_501(self, monkeypatch):
        case = _empty_case()
        monkeypatch.setattr("app.main.load_case", lambda cid: case)
        monkeypatch.setattr("app.main.save_case", lambda c, set_current=True: None)
        _patch_no_disk(monkeypatch)

        client = TestClient(app)
        resp = client.post("/api/reasoning/run", json={"case_id": "case-1", "selected_info_units": []})

        assert resp.status_code == 501
        body = resp.json()
        assert body["detail"]["status"] == "disabled"
        assert body["detail"]["stage"] == "reasoning"
        assert body["detail"]["phase"] == "phase1c"


# ---------------------------------------------------------------------------
# Extraction：Phase 2 起 adapter 已删除，走 typed pipeline
# ---------------------------------------------------------------------------


class TestExtractionStatusContract:
    def test_legacy_adapter_removed(self):
        """Phase 2：legacy `_normalize_extracted_unit` 适配器已彻底删除。"""
        import app.case_service as case_service_module

        assert not hasattr(case_service_module, "_normalize_extracted_unit")

    def test_extraction_docstring_declares_typed_pipeline(self):
        """run_extraction_agent docstring 声明 typed pipeline，且不再声称「尚未功能验证」。"""
        doc = inspect.getdoc(run_extraction_agent) or ""
        assert "typed ExtractionLLMResult" in doc
        assert "not yet functionally verified" not in doc
