"""文件上传 Evidence contract 回归测试（Phase 2 correction）。

验证：文件只作为 source/provenance 保存，不机械生成 Evidence entity。
Evidence 是否存在由 Extraction 根据材料内容与独立推理价值决定。

覆盖：
1. 初次上传（create_case_from_uploads）不因文件本身自动生成 Evidence。
2. Extraction 明确返回 Evidence 时正常保存。
3. append 路径（append_files_to_case）与 initial 路径语义一致。

不依赖真实 LLM（monkeypatch run_extraction_agent）与磁盘写（monkeypatch _store_uploaded_file）。
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from app import case_service
from app.case_service import append_files_to_case, create_case_from_uploads
from app.schemas import CaseData, Evidence


class FakeUpload:
    def __init__(self, filename, content, content_type="text/plain"):
        self.filename = filename
        self._content = content
        self.content_type = content_type

    async def read(self):
        return self._content


_EMPTY = {"persons": [], "events": [], "locations": [], "objects": [], "claims": [], "evidences": []}


def _patch(monkeypatch, extracted_dict=None):
    """mock run_extraction_agent（避免 LLM）与 _store_uploaded_file（避免磁盘写）。"""

    async def fake_extraction(case, req):
        return extracted_dict or _EMPTY

    monkeypatch.setattr(case_service, "run_extraction_agent", fake_extraction)
    monkeypatch.setattr(
        case_service,
        "_store_uploaded_file",
        lambda case_id, file_id, filename, raw: f"upload_files/{file_id}.bin",
    )


def _evidence(id_="evidence-1", desc="监控录像"):
    return Evidence(id=id_, description=desc)


def _script_upload():
    return FakeUpload("角色剧本.txt", "李明说：\"我昨晚在家。\"".encode("utf-8"))


class TestInitialUploadEvidenceContract:
    def test_plain_script_does_not_auto_generate_evidence(self, monkeypatch):
        """普通角色剧本上传后，不因文件本身自动出现 Evidence。"""
        _patch(monkeypatch)
        case = asyncio.run(create_case_from_uploads("剧本", [_script_upload()]))

        assert case.evidences == []
        # 文件仍作为 provenance 保存
        assert len(case.files) == 1
        assert case.files[0].filename == "角色剧本.txt"

    def test_file_level_meta_card_points_to_file(self, monkeypatch):
        """file-level 卡片 entity_ref 指向真实 file id（不再是 evidence id）。"""
        _patch(monkeypatch)
        case = asyncio.run(create_case_from_uploads("剧本", [_script_upload()]))

        file_card = next(c for c in case.meta_cards if c.entity_kind.value == "evidence")
        assert file_card.entity_ref == case.files[0].file_id


class TestAppendEvidenceContract:
    def test_append_does_not_auto_generate_evidence(self, monkeypatch):
        _patch(monkeypatch)
        case = CaseData(case_id="case-1", created_at=datetime(2024, 1, 1), updated_at=datetime(2024, 1, 1))

        result = asyncio.run(append_files_to_case(case, [_script_upload()]))

        assert result.evidences == []
        assert len(result.files) == 1

    def test_append_preserves_extraction_evidence(self, monkeypatch):
        _patch(monkeypatch, {**_EMPTY, "evidences": [_evidence()]})
        case = CaseData(case_id="case-1", created_at=datetime(2024, 1, 1), updated_at=datetime(2024, 1, 1))

        result = asyncio.run(append_files_to_case(case, [_script_upload()]))

        assert len(result.evidences) == 1
        assert result.evidences[0].description == "监控录像"


class TestInitialAppendConsistency:
    def test_both_paths_skip_mechanical_evidence(self, monkeypatch):
        """initial 与 append 路径语义一致：都不机械生成 Evidence。"""
        _patch(monkeypatch)
        initial = asyncio.run(create_case_from_uploads("剧本", [_script_upload()]))

        append_case = CaseData(case_id="case-1", created_at=datetime(2024, 1, 1), updated_at=datetime(2024, 1, 1))
        appended = asyncio.run(append_files_to_case(append_case, [_script_upload()]))

        assert initial.evidences == []
        assert appended.evidences == []
