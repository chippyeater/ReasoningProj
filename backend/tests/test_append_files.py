"""append_files_to_case 增量追加测试。

不依赖真实 LLM（monkeypatch run_extraction_agent）与磁盘写（monkeypatch _store_uploaded_file），
重点覆盖文件解析阶段（_extract_content / _guess_file_type / CaseFile 构造），
确保不再出现 _count_pages 这类 undefined name 运行时错误。

Phase 1b：adapted to 6 type-specific entities + new dict-based extraction result.
"""

import asyncio
from datetime import datetime

from app import case_service
from app.case_service import append_files_to_case
from app.schemas import CaseData, FileType, FileParseStatus, Claim, Person


class FakeUpload:
    def __init__(self, filename, content, content_type="text/plain"):
        self.filename = filename
        self._content = content
        self.content_type = content_type

    async def read(self):
        return self._content


def make_case():
    return CaseData(case_id="case-test", created_at=datetime(2024, 1, 1), updated_at=datetime(2024, 1, 1))


def make_person(id_="person-1", name="name", source="file-x"):
    return Person(
        id=id_, name=name,
        created_at=datetime(2024, 1, 1),
        updated_at=datetime(2024, 1, 1),
        source_refs=[source], confidence="medium", evidence_quote="quote",
    )


def make_claim(id_="claim-1", speaker="person-a", proposition="x", source="file-x"):
    return Claim(
        id=id_, claimant_ref=speaker, proposition=proposition,
        about_refs=[], source_refs=[source], confidence="medium", evidence_quote="quote",
        created_at=datetime(2024, 1, 1), updated_at=datetime(2024, 1, 1),
    )


def _patch(monkeypatch, extracted_dict=None):
    """mock run_extraction_agent（避免 LLM）与 _store_uploaded_file（避免磁盘写）。"""
    async def fake_extraction(case, req):
        return extracted_dict or {"persons": [], "events": [], "locations": [], "objects": [], "claims": [], "evidences": []}

    def fake_store(case_id, file_id, filename, raw):
        return f"upload_files/{file_id}.bin"

    monkeypatch.setattr(case_service, "run_extraction_agent", fake_extraction)
    monkeypatch.setattr(case_service, "_store_uploaded_file", fake_store)


class TestAppendFiles:
    def test_append_txt_file_no_nameerror(self, monkeypatch):
        """上传 txt 文件，验证文件解析阶段不再抛 undefined name（回归 _count_pages）。"""
        _patch(monkeypatch)
        case = make_case()
        upload = FakeUpload("笔录.txt", "这是张三的笔录内容".encode("utf-8"))

        result = asyncio.run(append_files_to_case(case, [upload]))

        assert len(result.files) == 1
        f = result.files[0]
        assert f.file_type == FileType.txt
        assert f.parse_status in (FileParseStatus.parsed, FileParseStatus.failed)
        assert f.page_count is None  # 不再调用不存在的 _count_pages
        assert f.filename == "笔录.txt"

    def test_append_pdf_file(self, monkeypatch):
        """上传 pdf 文件，走 _extract_content 的 pdf 分支，验证不抛错。"""
        _patch(monkeypatch)
        case = make_case()
        pdf = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF"
        upload = FakeUpload("合同.pdf", pdf, "application/pdf")

        result = asyncio.run(append_files_to_case(case, [upload]))

        assert len(result.files) == 1
        assert result.files[0].file_type == FileType.pdf
        assert result.files[0].page_count is None

    def test_append_docx_file(self, monkeypatch):
        """上传 docx 文件，走 docx 解析分支。"""
        _patch(monkeypatch)
        case = make_case()
        upload = FakeUpload("证据.docx", b"not-a-real-docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")

        result = asyncio.run(append_files_to_case(case, [upload]))

        assert len(result.files) == 1
        assert result.files[0].file_type == FileType.docx

    def test_dedup_merges_new_entities(self, monkeypatch):
        """新增 entity 按 id 集合去重合并。"""
        existing = make_person("person-old", "old", "file-old")
        new_unique = make_person("person-new", "new", "file-new")
        dup_of_existing = make_person("person-old", "old-dup", "file-old")  # 同 id

        extracted = {
            "persons": [new_unique, dup_of_existing],
            "events": [], "locations": [], "objects": [], "claims": [], "evidences": []
        }
        _patch(monkeypatch, extracted)
        case = make_case()
        case.persons = [existing]

        result = asyncio.run(append_files_to_case(case, [FakeUpload("b.txt", b"x")]))

        # 去重后只追加 new_unique，dup_of_existing 被过滤
        ids = [p.id for p in result.persons]
        assert ids == ["person-old", "person-new"]

    def test_empty_files_returns_case_unchanged(self, monkeypatch):
        """空文件列表应直接返回原 case，不触发任何解析。"""
        _patch(monkeypatch)
        case = make_case()
        result = asyncio.run(append_files_to_case(case, []))
        assert result is case
        assert result.files == []