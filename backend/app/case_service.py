"""Case service helpers for file ingestion and graph updates."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re

import openai
from io import BytesIO
from datetime import datetime
from pathlib import Path
from typing import Literal, Sequence, cast
from uuid import uuid4

from docx import Document
from fastapi import UploadFile
from pypdf import PdfReader
from starlette.datastructures import UploadFile as StarletteUploadFile

from app.case_store import append_case_log, case_upload_dir
from app.extraction import ExtractionStageError, materialize_extraction_output, parse_extraction_output
from app.schemas import (
    AgentRunLog,
    CaseData,
    CaseFile,
    CardPosition,
    Claim,
    ConfidenceLevel,
    DisplayLevel,
    Event,
    Evidence,
    ExtractionRequest,
    FileParseStatus,
    FileType,
    GenerateInferenceRequest,
    GraphEdge,
    GraphEdgeType,
    InferenceCard,
    InferenceDetail,
    InferenceType,
    IntentType,
    InteractionRequest,
    InteractionResponse,
    InteractionTrackRequest,
    Location,
    MetaCard,
    MetaDetail,
    MetaEntityKind,
    Object,
    Person,
    QARequest,
    QAResponse,
    ReasoningRequest,
    ReasoningResponse,
    ReferenceIntegrityError,
    RelationRequest,
    ResponseMode,
    RouteRequest,
    RouteResponse,
    RouterTask,
    SemanticRelation,
    SemanticRelationType,
    TimelineItem,
    TimelineItemType,
    TimelineView,
    UIAction,
    UIActionType,
    UpdateCardRequest,
    UpdateWorkspaceRequest,
    WorkspaceState,
)


class LLMRequestError(Exception):
    def __init__(self, message: str, status_code: int | None = None, provider_body: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.provider_body = provider_body


class RelationStageDisabledError(Exception):
    """Relation 模块被显式禁用时抛出（Phase 1c 前）。

    与「模块正常执行但关系数量为 0」严格区分：
    - 正常执行 → run_relation_agent 返回 (semantic_relations, edges)，允许为空列表；
    - 被禁用   → 抛出本异常，调用方（main.py）必须映射为非 200 状态（501）。

    Phase 1c 起 Relation 已真实实现，本异常仅作防御保留（例如未来按配置禁用）。
    """

    def __init__(self, stage: str = "relation", phase: str = "phase1b", reason: str = "") -> None:
        self.stage = stage
        self.phase = phase
        self.reason = reason or "Relation stage is disabled."
        super().__init__(self.reason)


class RelationStageError(Exception):
    """Relation 执行失败（error 态，Phase 1c）。

    与 disabled / success 严格区分：
    - success：正常执行，semantic_relations 允许为 []（正常执行但没找到）。
    - error：LLM/provider/parser failure。禁止 failure → fallback → []。
      本异常由 run_relation_agent 抛出，调用方（main.py）映射为 500。
    """

    def __init__(self, stage: str = "relation", phase: str = "phase1c", reason: str = "") -> None:
        self.stage = stage
        self.phase = phase
        self.reason = reason or "Relation stage failed (LLM/provider/parser error)."
        super().__init__(self.reason)


class ReasoningStageDisabledError(Exception):
    """Reasoning 模块被显式禁用时抛出（Phase 1c）。

    Phase 1b 的 reasoning 依赖 legacy graph（case.edges / legacy EdgeType /
    RelationRecord），Phase 1c 已 clean-break 删除这些 graph persistence。
    旧 reasoning 功能本就 not functionally verified；本 Phase 不重新设计 reasoning，
    只做 clean break + 显式 disabled boundary。调用方（main.py）映射为 501。
    """

    def __init__(self, stage: str = "reasoning", phase: str = "phase1c", reason: str = "") -> None:
        self.stage = stage
        self.phase = phase
        self.reason = reason or "Reasoning stage awaiting redesign; disabled after legacy graph clean-break."
        super().__init__(self.reason)


logger = logging.getLogger(__name__)

_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp"}
PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
PROMPT_FILE_BY_TASK = {
    "router": "router_system_prompt.md",
    "extraction": "extraction_system_prompt.md",
    "relation": "relation_system_prompt.md",
    "reasoning_selector": "reasoning_selector_prompt.md",
    "hypothesis_composer": "hypothesis_composer_prompt.md",
    "conflict_layer_composer": "conflict_layer_composer_prompt.md",
    "qa": "qa_system_prompt.md",
    "interaction": "interaction_system_prompt.md",
}


def _now() -> datetime:
    return datetime.utcnow()



def _read_prompt_markdown(filename: str, fallback: str) -> str:
    path = PROMPTS_DIR / filename
    if not path.exists():
        return fallback
    content = path.read_text(encoding="utf-8").strip()
    if "```text" in content:
        content = content.split("```text", 1)[1]
    if "```" in content:
        content = content.split("```", 1)[0]
    content = content.strip()
    return content or fallback


def _get_system_prompt(task: str) -> str:
    fallback = f"You are a {task} agent. Return strict JSON only."
    filename = PROMPT_FILE_BY_TASK.get(task)
    if not filename:
        return fallback
    return _read_prompt_markdown(filename, fallback)


def _append_agent_run(
    case: CaseData,
    task: RouterTask,
    subtask: str,
    input_snapshot: dict,
    output_snapshot: dict,
    status: str = "success",
    error: str | None = None,
) -> None:
    now = _now()
    normalized_status = cast(Literal["success", "failed", "fallback"], status if status in {"success", "failed", "fallback"} else "fallback")
    case.agent_runs.append(
        AgentRunLog(
            run_id=f"run-{uuid4().hex[:10]}",
            task=task,
            subtask=subtask,
            input_snapshot=input_snapshot,
            output_snapshot=output_snapshot,
            status=normalized_status,
            error=error,
            started_at=now,
            finished_at=now,
        )
    )



def _guess_file_type(filename: str, mime_type: str | None) -> FileType:
    suffix = Path(filename).suffix.lower()
    if suffix == ".pdf":
        return FileType.pdf
    if suffix == ".docx":
        return FileType.docx
    if suffix == ".txt":
        return FileType.txt
    if suffix in {".md", ".markdown"}:
        return FileType.markdown
    if suffix in _IMAGE_EXTENSIONS or (mime_type or "").startswith("image/"):
        return FileType.image
    return FileType.other


def _guess_parse_status(file_type: FileType, content: str) -> FileParseStatus:
    if file_type in {FileType.txt, FileType.markdown, FileType.image, FileType.pdf, FileType.docx}:
        return FileParseStatus.parsed
    if content.startswith("[Unsupported"):
        return FileParseStatus.failed
    return FileParseStatus.pending



def _preview_text(text: str, limit: int = 220) -> str:
    compact = " ".join((text or "").split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."


def _safe_decode(raw: bytes) -> str:
    for encoding in ("utf-8", "utf-8-sig", "gbk", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="ignore")


def _extract_content(filename: str, mime_type: str | None, raw: bytes) -> str:
    suffix = Path(filename).suffix.lower()

    if suffix in {".txt", ".md", ".markdown"}:
        return _safe_decode(raw)

    if suffix == ".docx":
        try:
            document = Document(BytesIO(raw))
            paragraphs = [paragraph.text.strip() for paragraph in document.paragraphs if paragraph.text.strip()]
            return "\n".join(paragraphs).strip() or "[No extractable DOCX text found]"
        except Exception as exc:
            return f"[DOCX parse failed: {exc}]"

    if suffix == ".pdf":
        try:
            reader = PdfReader(BytesIO(raw))
            extracted_pages: list[str] = []
            for index, page in enumerate(reader.pages, start=1):
                page_text = (page.extract_text() or "").strip()
                if page_text:
                    extracted_pages.append(f"[Page {index}]\n{page_text}")
            return "\n\n".join(extracted_pages).strip() or "[No extractable PDF text found]"
        except Exception as exc:
            return f"[PDF parse failed: {exc}]"

    if suffix in _IMAGE_EXTENSIONS or (mime_type or "").startswith("image/"):
        return f"[Image uploaded] {filename}"

    return f"[Unsupported uploaded file type: {suffix or (mime_type or 'unknown')}]"




def _store_uploaded_file(case_id: str, file_id: str, filename: str, raw: bytes) -> str:
    """Persist uploaded raw file under backend/data/{case_id}/upload_files."""
    upload_dir = case_upload_dir(case_id)
    upload_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(filename).suffix
    stored_name = f"{file_id}{suffix}"
    target = upload_dir / stored_name
    target.write_bytes(raw)
    return str(Path("upload_files") / stored_name)


def _read_uploaded_file_bytes(case_id: str, storage_path: str | None) -> bytes | None:
    if not storage_path:
        return None
    full_path = case_upload_dir(case_id).parent / storage_path
    if not full_path.exists() or not full_path.is_file():
        return None
    try:
        return full_path.read_bytes()
    except Exception:
        return None


def _file_content_for_extraction(case: CaseData, file_item: CaseFile) -> str:
    """
    Build extraction content payload for LLM.
    For text-like files, send full decoded content.
    For non-text files, keep parser summary placeholder.
    """
    raw = _read_uploaded_file_bytes(case.case_id, file_item.storage_path)
    if raw is None:
        return file_item.preview_text or ""
    full_content = _extract_content(file_item.filename, None, raw)
    if file_item.file_type in {FileType.txt, FileType.markdown, FileType.docx, FileType.pdf}:
        return full_content
    return file_item.preview_text or full_content
def _create_meta_card(title: str, summary: str, file_id: str, entity_kind: MetaEntityKind, entity_ref: str) -> MetaCard:
    now = _now()
    return MetaCard(
        id=f"meta-{uuid4().hex[:10]}",
        title=title,
        summary=summary,
        display_level=DisplayLevel.level_1,
        position=CardPosition(x=80, y=80),
        source_file_ids=[file_id],
        created_at=now,
        updated_at=now,
        entity_kind=entity_kind,
        entity_ref=entity_ref,
        detail=MetaDetail(name=title, description=summary, tags=["auto-generated"]),
    )


def _mode_to_inference_type(mode: str) -> InferenceType:
    return {
        "hypothesis": InferenceType.hypothesis,
        "conclusion": InferenceType.conclusion,
        "conflict_check": InferenceType.conflict,
        "missing_evidence": InferenceType.missing_evidence,
    }.get(mode, InferenceType.hypothesis)


def _create_inference_card(claim: str, selected_ids: list[str], mode: str, title: str | None = None) -> InferenceCard:
    now = _now()
    inference_type = _mode_to_inference_type(mode)

    fallback_title = {
        InferenceType.hypothesis: "Generated Hypothesis",
        InferenceType.conclusion: "Generated Conclusion",
        InferenceType.conflict: "Potential Conflict",
        InferenceType.missing_evidence: "Missing Evidence Hint",
    }[inference_type]

    card_title = (title or "").strip() or fallback_title

    return InferenceCard(
        id=f"infer-{uuid4().hex[:10]}",
        title=card_title,
        summary=claim,
        display_level=DisplayLevel.level_2,
        position=CardPosition(x=420, y=180),
        source_file_ids=[],
        created_at=now,
        updated_at=now,
        inference_type=inference_type,
        detail=InferenceDetail(
            claim=claim,
            reasoning_steps=["Generated by LLM."],
            supporting_card_ids=selected_ids,
        ),
    )


def create_empty_case(case_title: str, case_id: str | None = None) -> CaseData:
    now = _now()
    normalized_title = (case_title or "未命名案件").strip() or "未命名案件"
    return CaseData(
        case_id=case_id or f"case-{uuid4().hex[:8]}",
        case_title=normalized_title,
        created_at=now,
        updated_at=now,
        files=[],
        meta_cards=[],
        inference_cards=[],
        workspace_state=WorkspaceState(),
    )


async def create_case_from_uploads(
    case_title: str,
    files: Sequence[UploadFile | StarletteUploadFile],
) -> CaseData:
    case = create_empty_case(case_title=case_title)
    if not files:
        return case

    for upload in files:
        raw = await upload.read()
        filename = upload.filename or "uploaded-file"
        mime_type = upload.content_type
        file_type = _guess_file_type(filename, mime_type)
        content = _extract_content(filename, mime_type, raw)
        parse_status = _guess_parse_status(file_type, content)
        now = _now()
        file_id = f"file-{uuid4().hex[:10]}"
        stored_path = _store_uploaded_file(case.case_id, file_id, filename, raw)

        case.files.append(
            CaseFile(
                file_id=file_id,
                filename=filename,
                file_type=file_type,
                file_size=len(raw),
                storage_path=stored_path,
                uploaded_at=now,
                parse_status=parse_status,
                preview_text=_preview_text(content),
                page_count=None,
                error_message=content if parse_status == FileParseStatus.failed else None,
            )
        )

        # 文件只作为 source/provenance 保存（case.files）；不机械生成 Evidence entity。
        # Evidence 是否存在由 Extraction 根据材料内容与独立推理价值决定。
        # file-level meta_card 仅作前端文件卡片展示，entity_ref 指向真实 file_id。
        case.meta_cards.append(
            _create_meta_card(
                title=filename,
                summary=_preview_text(content, 96) or "No extracted text.",
                file_id=file_id,
                entity_kind=MetaEntityKind.evidence,
                entity_ref=file_id,
            )
        )

    case.updated_at = _now()
    await run_extraction_agent(case, ExtractionRequest(case_id=case.case_id, file_ids=[f.file_id for f in case.files]))
    return case



async def append_files_to_case(
    case: CaseData,
    files: Sequence[UploadFile | StarletteUploadFile],
) -> CaseData:
    """Append uploaded files to an existing case with incremental extraction.

    - 新增文件单独存储 + 加入 case.files
    - 对新增文件单独跑 extraction agent（不重跑已有文件）
    - 新抽取的 entities 按 id 集合去重合并
    - meta_cards 增量追加，不重建
    """
    new_file_ids: list[str] = []
    for upload in files:
        raw = await upload.read()
        filename = upload.filename or "uploaded-file"
        mime_type = upload.content_type
        file_type = _guess_file_type(filename, mime_type)
        content = _extract_content(filename, mime_type, raw)
        parse_status = _guess_parse_status(file_type, content)
        now = _now()
        file_id = f"file-{uuid4().hex[:10]}"
        stored_path = _store_uploaded_file(case.case_id, file_id, filename, raw)

        case.files.append(
            CaseFile(
                file_id=file_id,
                filename=filename,
                file_type=file_type,
                file_size=len(raw),
                storage_path=stored_path,
                uploaded_at=now,
                parse_status=parse_status,
                preview_text=_preview_text(content),
                page_count=None,
                error_message=content if parse_status == FileParseStatus.failed else None,
            )
        )
        new_file_ids.append(file_id)

    if not new_file_ids:
        return case

    # 对新增文件单独跑 extraction
    extraction_req = ExtractionRequest(case_id=case.case_id, file_ids=new_file_ids)
    extracted = await run_extraction_agent(case, extraction_req)

    # 按 id 集合去重合并到 case 各 entity 列表
    def _merge(new_items, existing_items):
        existing_ids = {item.id for item in existing_items}
        return [item for item in new_items if item.id not in existing_ids]

    case.persons.extend(_merge(extracted["persons"], case.persons))
    case.events.extend(_merge(extracted["events"], case.events))
    case.locations.extend(_merge(extracted["locations"], case.locations))
    case.objects.extend(_merge(extracted["objects"], case.objects))
    case.claims.extend(_merge(extracted["claims"], case.claims))
    case.evidences.extend(_merge(extracted["evidences"], case.evidences))

    # 增量追加 meta_cards（append-only）
    _sync_meta_cards_from_entities(case, extracted, append_only=True)

    case.updated_at = _now()
    return case

def upsert_case_from_payload(existing: CaseData | None, payload: CaseData) -> CaseData:
    if existing is None:
        return payload
    return payload.model_copy(update={"created_at": existing.created_at, "updated_at": _now()})


def update_card(case: CaseData, card_id: str, patch: UpdateCardRequest) -> tuple[CaseData, bool]:
    updated = False

    for card in case.meta_cards:
        if card.id != card_id:
            continue
        if patch.title is not None:
            card.title = patch.title
        if patch.summary is not None:
            card.summary = patch.summary
        if patch.display_level is not None:
            card.display_level = patch.display_level
        if patch.position is not None:
            card.position = patch.position
        if patch.ui_state is not None:
            card.ui_state = patch.ui_state
        if patch.detail is not None:
            data = card.detail.model_dump()
            data.update(patch.detail)
            card.detail = MetaDetail.model_validate(data)
        card.updated_at = _now()
        updated = True
        break

    if not updated:
        for card in case.inference_cards:
            if card.id != card_id:
                continue
            if patch.title is not None:
                card.title = patch.title
            if patch.summary is not None:
                card.summary = patch.summary
            if patch.display_level is not None:
                card.display_level = patch.display_level
            if patch.position is not None:
                card.position = patch.position
            if patch.ui_state is not None:
                card.ui_state = patch.ui_state
            if patch.decision is not None:
                card.decision = patch.decision
            if patch.detail is not None:
                data = card.detail.model_dump()
                data.update(patch.detail)
                card.detail = InferenceDetail.model_validate(data)
            card.updated_at = _now()
            updated = True
            break

    if updated:
        case.updated_at = _now()
    return case, updated


def update_workspace(case: CaseData, patch: UpdateWorkspaceRequest) -> CaseData:
    workspace = case.workspace_state
    if patch.current_view is not None:
        workspace.current_view = patch.current_view
    if patch.selected_card_ids is not None:
        workspace.selected_card_ids = patch.selected_card_ids
    if patch.focused_card_id is not None:
        workspace.focused_card_id = patch.focused_card_id
    if patch.expanded_card_ids is not None:
        workspace.expanded_card_ids = patch.expanded_card_ids
    if patch.pinned_card_ids is not None:
        workspace.pinned_card_ids = patch.pinned_card_ids
    if patch.viewport is not None:
        workspace.viewport = patch.viewport

    case.workspace_state = workspace
    case.updated_at = _now()
    return case


def _load_local_env() -> None:
    if os.getenv("_REASONING_ENV_LOADED") == "1":
        return

    env_path = Path(__file__).resolve().parent.parent.parent / ".env"
    if env_path.exists():
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))

    os.environ["_REASONING_ENV_LOADED"] = "1"


def _provider_config() -> tuple[str, str, str]:
    """返回 (api_key, base_url, model)。

    base_url 为 OpenAI 兼容 endpoint 的完整 base（不含 /chat/completions）：
    - Azure AI Foundry 项目 endpoint（*.services.ai.azure.com）自动补 /openai/v1
    - 未配置 base_url 时返回空串，由 openai SDK 使用默认 api.openai.com/v1
    """
    _load_local_env()
    api_key = (
        os.getenv("AZURE_OPENAI_API_KEY", "").strip()
        or os.getenv("OPENAI_API_KEY", "").strip()
        or os.getenv("GITHUB_TOKEN", "").strip()
        or os.getenv("GITHUB_MODELS_TOKEN", "").strip()
    )
    base_url = (
        os.getenv("AZURE_OPENAI_ENDPOINT", "").strip()
        or os.getenv("OPENAI_BASE_URL", "").strip()
    ).rstrip("/")
    model = (
        os.getenv("AZURE_OPENAI_MODEL", "").strip()
        or os.getenv("OPENAI_MODEL", "").strip()
        or "gpt-5-mini"
    )
    if base_url and "services.ai.azure.com" in base_url and not base_url.endswith("/openai/v1"):
        base_url = f"{base_url}/openai/v1"
    return api_key, base_url, model


def _extract_json(text: str) -> dict:
    content = (text or "").strip()
    if "```json" in content:
        content = content.split("```json", 1)[1]
    if "```" in content:
        content = content.split("```", 1)[0]
    content = content.strip()
    return json.loads(content)


def _normalize_optional_text(value: object) -> str | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw or raw.lower() in {"none", "null", "n/a", "unknown"}:
        return None
    return raw


def _call_llm_sync(system_prompt: str, user_prompt: str) -> dict:
    api_key, base_url, model = _provider_config()
    if not api_key:
        raise LLMRequestError("No API token configured")

    client = openai.OpenAI(
        base_url=base_url or None,
        api_key=api_key,
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    # provider boundary：任何 LLM 调用/响应结构异常统一转成 LLMRequestError 向上抛，
    # 由上层 agent（run_extraction_agent 等）记录日志并决定 fallback。此处 re-raise，
    # 不吞异常、不静默降级，因此保留 broad Exception。
    try:
        response = client.chat.completions.create(
            model=model,
            messages=messages,
        )
        content = response.choices[0].message.content or ""
    except Exception as exc:
        status_code: int | None = None
        provider_body = ""
        for attr in ("status_code", "code"):
            candidate = getattr(exc, attr, None)
            if candidate:
                status_code = candidate
                break
        resp_obj = getattr(exc, "response", None)
        if resp_obj is not None:
            try:
                provider_body = resp_obj.text
            except Exception:
                provider_body = str(exc)
        else:
            provider_body = str(exc)
        raise LLMRequestError(
            message=f"LLM request failed: {exc}",
            status_code=status_code,
            provider_body=provider_body,
        ) from exc

    if isinstance(content, list):
        text_parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text_parts.append(str(item.get("text", "")))
        content = "\n".join(text_parts)

    if not isinstance(content, str):
        content = str(content)

    # JSON 解析失败不是 provider 错误：LLM 可能返回非 JSON 文本。记录 warning，
    # 保留 raw_reply 供上层日志记录，parsed_json 置空让上层走 fallback。
    parsed_json: dict = {}
    try:
        parsed_json = _extract_json(content)
    except ValueError:
        logger.warning("LLM 返回非 JSON 内容（model=%s, length=%d），保留 raw_reply 走 fallback", model, len(content))

    return {
        "parsed_json": parsed_json,
        "raw_reply": content,
        "request_meta": {
            "model": model,
            "base_url": base_url or "https://api.openai.com/v1",
        },
        "response_meta": {
            "http_status": 200,
            "provider_body_preview": "",
        },
    }


async def generate_inference_with_llm(
    case: CaseData,
    request: GenerateInferenceRequest,
) -> tuple[list[InferenceCard], list[GraphEdge], WorkspaceState, str]:
    """Phase 1c：Reasoning / inference 生成被显式禁用。

    Phase 1b 的 reasoning/inference 依赖 legacy graph（case.edges / legacy EdgeType /
    RelationRecord），Phase 1c 已 clean-break 删除这些 graph persistence。
    旧功能本就 not functionally verified；本 Phase 不重新设计 reasoning，只做
    clean break + 显式 disabled boundary。调用方（main.py）映射为 501。
    """
    _append_agent_run(
        case,
        RouterTask.reasoning,
        "generate_inference",
        request.model_dump(),
        {"status": "disabled", "phase": "phase1c"},
        status="fallback",
        error="ReasoningStageDisabledError: reasoning/inference stage awaiting redesign (Phase 1c clean-break)",
    )
    append_case_log(
        case.case_id,
        "llm",
        {
            "stage": "reasoning",
            "status": "disabled",
            "message": "reasoning/inference stage disabled in Phase 1c; awaiting redesign after legacy graph clean-break",
        },
    )
    raise ReasoningStageDisabledError()


async def route_task(case: CaseData, req: RouteRequest) -> RouteResponse:
    text = req.user_input.strip().lower()
    task = RouterTask.qa
    subtask = "answer"
    requires_tool = False

    if any(k in text for k in ["upload", "parse", "extract", "上传", "提取"]):
        task = RouterTask.extraction
        subtask = "extract_entities"
        requires_tool = True
    elif any(k in text for k in ["relation", "关系"]):
        task = RouterTask.relation
        subtask = "build_relations"
        requires_tool = True
    elif any(k in text for k in ["reason", "hypothesis", "推理", "假设", "冲突"]):
        task = RouterTask.reasoning
        subtask = "generate_reasoning"
        requires_tool = True
    elif any(k in text for k in ["highlight", "open", "switch", "rearrange", "高亮", "切换", "重排"]):
        task = RouterTask.interaction
        subtask = "ui_action"

    response = RouteResponse(task=task, subtask=subtask, target_scope=req.current_selection, requires_tool=requires_tool)
    _append_agent_run(case, RouterTask.interaction, "router", req.model_dump(), response.model_dump(mode="json"))
    return response



# ----------------------------------------------------------------------
# Core Info Model helpers（Phase 2）
# 新模型：Person / Event / Location / Object / Claim / Evidence。
# Source ≠ Reality：抽取阶段只记录 source 表达的内容，不做世界事实推断。
# Confidence 仅表示抽取置信度，不表示真实性（矛盾不降 confidence）。
#
# Phase 2 起，Extraction LLM 直接输出 typed ExtractionLLMResult（见 app/extraction.py），
# legacy InfoUnit schema 与 `_normalize_extracted_unit` 适配器已彻底删除。
# ----------------------------------------------------------------------

def _confidence(value: str | None) -> ConfidenceLevel:
    raw = (value or "").strip().lower()
    if raw == "high":
        return ConfidenceLevel.high
    if raw == "low":
        return ConfidenceLevel.low
    return ConfidenceLevel.medium


def _validate_references(case: CaseData) -> list[str]:
    """跨实体引用完整性校验（Phase 1c）。

    通过 `CaseData.entity_by_id(ref)` 查找目标 entity，再以实例类型判定 kind。
    ID 前缀只是命名约定，不作为真实性判断依据。

    - Claim.claimant_ref：可为 None（None 表示身份未解析，已存 claimant_text）；
      若非 None，必须是 case.persons 之一。
    - Claim.participant_refs[*]：每一项必须指向 Person。
    - Claim.location_ref：若非 None，必须指向 Location。
    - Claim.object_refs[*]：每一项必须指向 Object。
    - Event.participant_refs[*]：每一项必须指向 Person。
    - Event.location_ref：若非 None，必须指向 Location。
    - Event.object_refs[*]：每一项必须指向 Object。
    - Claim.about_refs[*] / Evidence.about_refs[*]：必须存在（任意 entity kind 都允许）。

    返回非法引用 id 列表供上层 fallback 记录，不抛错。
    """
    invalid: list[str] = []

    def _must_be(ref: str | None, kind: type, owner: str, field: str) -> None:
        if ref is None:
            return
        entity = case.entity_by_id(ref)
        if entity is None or not isinstance(entity, kind):
            invalid.append(f"{owner}:{field}:{ref}")

    for event in case.events:
        for participant_ref in event.participant_refs:
            _must_be(participant_ref, Person, f"event:{event.id}", "participant_ref")
        _must_be(event.location_ref, Location, f"event:{event.id}", "location_ref")
        for obj_ref in event.object_refs:
            _must_be(obj_ref, Object, f"event:{event.id}", "object_ref")
    for claim in case.claims:
        _must_be(claim.claimant_ref, Person, f"claim:{claim.id}", "claimant_ref")
        for participant_ref in claim.participant_refs:
            _must_be(participant_ref, Person, f"claim:{claim.id}", "participant_ref")
        _must_be(claim.location_ref, Location, f"claim:{claim.id}", "location_ref")
        for obj_ref in claim.object_refs:
            _must_be(obj_ref, Object, f"claim:{claim.id}", "object_ref")
        for ref in claim.about_refs:
            if case.entity_by_id(ref) is None:
                invalid.append(f"claim:{claim.id}:about_ref:{ref}")
    for evidence in case.evidences:
        for ref in evidence.about_refs:
            if case.entity_by_id(ref) is None:
                invalid.append(f"evidence:{evidence.id}:about_ref:{ref}")
    return invalid


# ----------------------------------------------------------------------
# Timeline — runtime projection（Phase 3）
#
# Timeline 只消费已有 Core Entity（Event / Claim）的 time，不重新抽取事实、
# 不创建新的事实实体、不写回 CaseData、不调用 LLM、不做 temporal conflict
# detection。前端通过 item_ref 回查 Core Entity 获取展示内容。
# ----------------------------------------------------------------------


# canonical sort_value 唯一合法格式：YYYY-MM-DDTHH:MM（严格匹配）
_CANONICAL_SORT_VALUE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$")


def canonical_sort_value(value: str | None) -> str | None:
    """返回可直接机器比较的 canonical sort_value；非 canonical 一律视为不可排序。

    Contract（v1）：`sort_value` 只在 source / context 足以可靠得到完整
    「年 + 月 + 日 + 时间」时才生成，格式统一为 `YYYY-MM-DDTHH:MM`。

    历史数据中可能存在 legacy 自然语言 / partial key（如 `"3月12日晚8点"`、
    `"20:03"`、`"当晚"`）。这些值**不是** canonical temporal key，若直接参与
    字典序比较会产生错误的时间顺序，因此在此统一降级为 `None`。

    本函数只做格式识别，不做解析、不做补全、不猜测、不改写历史 `case.json`。
    """
    if value is None:
        return None
    if _CANONICAL_SORT_VALUE_RE.match(value):
        return value
    return None


def _timeline_sort_key(item: TimelineItem) -> tuple[bool, str, str, str]:
    """确定性排序 key。

    - 第一段 `sort_value is None`：把「无可排序 canonical time」的 item
      稳定排到所有可排序时间之后（避免 `None` 被当成空串排到最前）。
      非 canonical 的 legacy 值经 `canonical_sort_value` 同样归一为 `None`。
    - 第二段：canonical `YYYY-MM-DDTHH:MM` 直接字典序比较（等价于时间先后）。
    - 第三、四段：item_type + item_ref 做 deterministic tie-break，
      保证相同时间点的顺序稳定。
    """
    sort_value = canonical_sort_value(item.time.sort_value)
    return (sort_value is None, sort_value or "", item.item_type.value, item.item_ref)


def build_timeline(case: CaseData) -> TimelineView:
    """把 CaseData 的 Event / Claim 投影为 Timeline View Model（纯函数）。

    - 只纳入 time != None 的 Event / Claim；time=None 暂不进入 Timeline。
    - Event / Claim 混排，approximate 时间仍可进入（precision 不参与过滤）。
    - 不修改 case：只读取，不写回。
    """
    items: list[TimelineItem] = []
    for event in case.events:
        if event.time is not None:
            items.append(TimelineItem(
                item_type=TimelineItemType.event,
                item_ref=event.id,
                time=event.time,
            ))
    for claim in case.claims:
        if claim.time is not None:
            items.append(TimelineItem(
                item_type=TimelineItemType.claim,
                item_ref=claim.id,
                time=claim.time,
            ))
    items.sort(key=_timeline_sort_key)
    return TimelineView(case_id=case.case_id, items=items)


def _sync_meta_cards_from_entities(
    case: CaseData,
    extracted: dict[str, list],
    append_only: bool = False,
) -> None:
    """根据 6 类 entity 同步 meta_cards。"""
    kind_map: dict[type, MetaEntityKind] = {
        Person: MetaEntityKind.person,
        Event: MetaEntityKind.event,
        Location: MetaEntityKind.location,
        Object: MetaEntityKind.object,
        Claim: MetaEntityKind.claim,
        Evidence: MetaEntityKind.evidence,
    }
    id_attr_map: dict[type, str] = {
        Person: "name",
        Location: "name",
        Object: "name",
        Event: "description",
        Claim: "proposition",
        Evidence: "description",
    }

    new_cards: list[MetaCard] = []
    offset = len(case.meta_cards) if append_only else 0
    counter = 0

    def _entity_label(entity) -> tuple[str, str, str]:
        label_attr = id_attr_map.get(type(entity), "name")
        label = getattr(entity, label_attr, "") or entity.id
        quote = entity.evidence_quote or ""
        return label, quote, type(entity).__name__

    for collection in (
        extracted.get("persons", []),
        extracted.get("events", []),
        extracted.get("locations", []),
        extracted.get("objects", []),
        extracted.get("claims", []),
        extracted.get("evidences", []),
    ):
        for entity in collection:
            label, quote, kind_name = _entity_label(entity)
            now = _now()
            kind = kind_map.get(type(entity), MetaEntityKind.evidence)
            x = 80 + ((counter + offset) % 4) * 260
            y = 100 + ((counter + offset) // 4) * 170
            new_cards.append(
                MetaCard(
                    id=f"meta-{entity.id}",
                    title=label,
                    summary=quote[:160] or label,
                    display_level=DisplayLevel.level_1,
                    position=CardPosition(x=x, y=y),
                    source_file_ids=list(entity.source_refs),
                    created_at=now,
                    updated_at=now,
                    entity_kind=kind,
                    entity_ref=entity.id,
                    detail=MetaDetail(
                        name=label,
                        description=quote[:280] or label,
                        tags=[f"confidence:{entity.confidence.value}", "extracted"],
                        attributes={"entity_kind": kind_name.lower()},
                    ),
                )
            )
            counter += 1

    if append_only:
        existing_refs = {card.entity_ref for card in case.meta_cards}
        for card in new_cards:
            if card.entity_ref not in existing_refs:
                case.meta_cards.append(card)
    else:
        # 重建时，保留 file-level evidence meta cards（来自 create_case_from_uploads）
        preserved = [
            c for c in case.meta_cards
            if c.entity_kind == MetaEntityKind.evidence
            and not any(c.entity_ref == nc.entity_ref for nc in new_cards)
        ]
        case.meta_cards = preserved + new_cards


_ENTITY_BUCKET_KEYS = ("persons", "events", "locations", "objects", "claims", "evidences")


def build_extraction_context(case: CaseData, req: ExtractionRequest) -> dict:
    """Phase 2：构造 Extraction LLM 的输入 context。

    只传本次需要抽取的 source 内容与必要 metadata，不传：
    - 整个 CaseData / 已有 entities / Relation / GraphEdge
    - UI state / reasoning state / case_title / parse_status 等无关字段
    """
    file_ids = req.file_ids or [f.file_id for f in case.files]
    target_files = [f for f in case.files if f.file_id in file_ids]
    return {
        "files": [
            {
                "file_id": f.file_id,
                "filename": f.filename,
                "file_type": f.file_type.value,
                "content": _file_content_for_extraction(case, f),
            }
            for f in target_files
        ],
        "raw_text": req.raw_text or "",
        "context": req.context or "",
    }


async def run_extraction_agent(
    case: CaseData,
    req: ExtractionRequest,
) -> dict[str, list]:
    """Phase 2 extraction 入口。

    pipeline：raw source → Extraction LLM → typed ExtractionLLMResult → id 物化
    → reference validation → CaseData。不再经过 legacy InfoUnit adapter。

    三态语义：
    - success：正常执行；entities 允许为空（正常执行但 source 无可抽取实体）。
    - error：LLM/provider/parser/reference failure → 抛 ExtractionStageError /
      ReferenceIntegrityError，禁止静默 fallback 到 []。
    """
    file_ids = req.file_ids or [f.file_id for f in case.files]
    context = build_extraction_context(case, req)
    system_prompt = _get_system_prompt("extraction")
    user_prompt = (
        "Input payload:\n"
        + json.dumps(context, ensure_ascii=False, indent=2)
        + "\n\nReturn strict JSON with persons, events, locations, objects, claims, evidences."
    )

    try:
        llm_result = await asyncio.to_thread(_call_llm_sync, system_prompt, user_prompt)
    except LLMRequestError as exc:
        append_case_log(case.case_id, "llm", {
            "stage": "extraction",
            "status": "error",
            "message": "extraction-provider-failure",
            "error_code": exc.status_code,
            "request_snapshot": context,
            "provider_body_preview": (exc.provider_body or "")[:4000],
        })
        raise ExtractionStageError(reason=f"LLM/provider failure: {exc}") from exc

    parsed = llm_result.get("parsed_json", {})
    raw_reply = str(llm_result.get("raw_reply", ""))
    request_meta = llm_result.get("request_meta", {})
    response_meta = llm_result.get("response_meta", {})

    # 1. 严格 typed 解析（拒绝 legacy schema）+ 2. id 物化（全局唯一 + 引用重写）
    try:
        output = parse_extraction_output(parsed)
        extracted = materialize_extraction_output(output, file_ids)
    except ExtractionStageError:
        append_case_log(case.case_id, "llm", {
            "stage": "extraction",
            "status": "error",
            "message": "extraction-parser-failure",
            "request_meta": request_meta,
            "request_snapshot": context,
            "raw_reply": raw_reply[:4000],
            "parsed_snapshot": parsed,
            "response_meta": response_meta,
        })
        raise

    # 3. reference validation：invalid ref 显式 error，不静默删除
    combined = case.model_copy()
    combined.persons = [*case.persons, *extracted["persons"]]
    combined.events = [*case.events, *extracted["events"]]
    combined.locations = [*case.locations, *extracted["locations"]]
    combined.objects = [*case.objects, *extracted["objects"]]
    combined.claims = [*case.claims, *extracted["claims"]]
    combined.evidences = [*case.evidences, *extracted["evidences"]]
    invalid_refs = _validate_references(combined)
    if invalid_refs:
        append_case_log(case.case_id, "llm", {
            "stage": "extraction",
            "status": "error",
            "message": "extraction-reference-integrity-failure",
            "invalid_refs": invalid_refs,
            "request_meta": request_meta,
            "request_snapshot": context,
            "raw_reply": raw_reply[:4000],
            "parsed_snapshot": parsed,
        })
        raise ReferenceIntegrityError(
            f"extraction produced invalid references: {invalid_refs[:8]}",
            invalid_refs=invalid_refs,
        )

    # 4. 合并进 case
    case.persons.extend(extracted["persons"])
    case.events.extend(extracted["events"])
    case.locations.extend(extracted["locations"])
    case.objects.extend(extracted["objects"])
    case.claims.extend(extracted["claims"])
    case.evidences.extend(extracted["evidences"])
    _sync_meta_cards_from_entities(case, extracted)
    case.updated_at = _now()

    total = sum(len(extracted[key]) for key in _ENTITY_BUCKET_KEYS)
    append_case_log(case.case_id, "llm", {
        "stage": "extraction",
        "status": "success",
        "message": "extraction-success" if total else "extraction-success-empty",
        "request_meta": request_meta,
        "request_snapshot": context,
        "raw_reply": raw_reply[:4000],
        "parsed_snapshot": parsed,
        "response_meta": response_meta,
        "entity_counts": {k: len(v) for k, v in extracted.items()},
    })

    _append_agent_run(
        case,
        RouterTask.extraction,
        "extract_entities",
        req.model_dump(),
        {"counts": {k: len(v) for k, v in extracted.items()}},
    )
    return extracted


async def run_relation_agent(
    case: CaseData,
    req: RelationRequest,
) -> tuple[list[SemanticRelation], list[GraphEdge]]:
    """Phase 1c：真实 semantic relation 抽取（LLM）。

    三态语义（严格区分，不混淆）：
    - disabled：本 Phase 实现后不再 disabled（RelationStageDisabledError 保留作防御）。
    - success：正常执行；semantic_relations 允许为 []（正常执行但没找到关系）。
    - error：LLM/provider/parser failure → 抛 RelationStageError，绝不 fallback 到 []。

    端点约束（第一版，在 _build_semantic_relations 内强制）：
    - supports：source ∈ {Claim, Evidence}，target = Claim。
    - contradicts：source = Claim，target = Claim（对称 canonical dedup）。

    Relation stage 不负责：世界事实判断 / 身份猜测 / 时间推理 / hypothesis。
    """
    entity_ids = set(req.info_unit_ids) if req.info_unit_ids else None
    entities = [
        _compact_entity_for_relation(entity)
        for entity in case.all_entities()
        if entity_ids is None or entity.id in entity_ids
    ]
    request_snapshot = {
        "case_title": case.case_title,
        "entities": entities,
    }
    user_prompt = (
        "Input payload:\n"
        + json.dumps(request_snapshot, ensure_ascii=False, indent=2)
        + "\n\n"
        + "Return strict JSON with key: semantic_relations. "
        + "Each item: {source_id, target_id, relation_type, detection_confidence, rationale}. "
        + "relation_type must be one of: supports, contradicts."
    )

    try:
        llm_result = await asyncio.to_thread(_call_llm_sync, _get_system_prompt("relation"), user_prompt)
    except LLMRequestError as exc:
        _append_agent_run(
            case, RouterTask.relation, "build_relations", req.model_dump(),
            {"status": "error", "phase": "phase1c"},
            status="failed",
            error=f"RelationStageError: LLM/provider failure: {exc}",
        )
        raise RelationStageError(reason=f"LLM/provider failure: {exc}") from exc

    parsed = llm_result.get("parsed_json", {})
    raw_reply = str(llm_result.get("raw_reply", ""))
    response_meta = llm_result.get("response_meta", {})
    request_meta = llm_result.get("request_meta", {})

    raw_relations = parsed.get("semantic_relations") if isinstance(parsed, dict) else None
    if not isinstance(raw_relations, list):
        # parser failure → error，禁止 fallback 到 []。
        _append_agent_run(
            case, RouterTask.relation, "build_relations", req.model_dump(),
            {"status": "error", "phase": "phase1c"},
            status="failed",
            error="RelationStageError: LLM response did not contain a valid semantic_relations list",
        )
        append_case_log(case.case_id, "llm", {
            "stage": "relation", "status": "error",
            "message": "relation-parser-failure",
            "raw_reply": raw_reply[:4000],
            "parsed_snapshot": parsed,
            "response_meta": response_meta,
        })
        raise RelationStageError(reason="LLM response did not contain a valid semantic_relations list")

    semantic_relations = _build_semantic_relations(case, raw_relations)

    # 持久化 semantic relations（success 态，允许为空）
    case.semantic_relations.extend(semantic_relations)
    case.updated_at = _now()

    # semantic GraphEdge 是 runtime projection，不持久化；返回本次投影供前端即时更新
    edges = _semantic_edges_for(semantic_relations)

    _append_agent_run(
        case, RouterTask.relation, "build_relations", req.model_dump(),
        {
            "status": "success",
            "phase": "phase1c",
            "semantic_relation_count": len(semantic_relations),
        },
        status="success",
    )
    append_case_log(case.case_id, "llm", {
        "stage": "relation", "status": "success",
        "message": "relation-success" if semantic_relations else "relation-success-empty",
        "request_meta": request_meta,
        "request_snapshot": request_snapshot,
        "raw_reply": raw_reply[:4000],
        "parsed_snapshot": parsed,
        "response_meta": response_meta,
        "semantic_relation_count": len(semantic_relations),
    })
    return semantic_relations, edges


# ----------------------------------------------------------------------
# Phase 1c — Graph projection（runtime，不持久化）
# ----------------------------------------------------------------------

def _compact_entity_for_relation(entity) -> dict[str, object]:
    """entity → relation prompt 投影。

    只输出 relation 判断需要的信息：id / kind / 文本 / evidence_quote。
    """
    base: dict[str, object] = {
        "id": entity.id,
        "evidence_quote": entity.evidence_quote or "",
    }
    if isinstance(entity, Person):
        base.update({"kind": "person", "text": entity.name})
    elif isinstance(entity, Location):
        base.update({"kind": "location", "text": entity.name})
    elif isinstance(entity, Object):
        base.update({"kind": "object", "text": entity.name})
    elif isinstance(entity, Event):
        base.update({"kind": "event", "text": entity.description})
    elif isinstance(entity, Claim):
        base.update({"kind": "claim", "text": entity.proposition})
    elif isinstance(entity, Evidence):
        base.update({"kind": "evidence", "text": entity.description})
    else:
        base.update({"kind": "unknown", "text": entity.id})
    return base


def _checked_ref(case: CaseData, ref: str, kind: type, owner: str, field: str) -> None:
    """严格 ref 校验：entity_by_id + isinstance。非法即抛 ReferenceIntegrityError，不静默。"""
    entity = case.entity_by_id(ref)
    if entity is None or not isinstance(entity, kind):
        raise ReferenceIntegrityError(
            f"dangling/type-mismatched ref: {owner}:{field}={ref}",
            invalid_refs=[f"{owner}:{field}:{ref}"],
        )


def build_structural_edges(case: CaseData) -> list[GraphEdge]:
    """从 core entity refs 投影 structural GraphEdge（deterministic，0 LLM token）。

    严格 ref 校验：任何 dangling ref 或类型不匹配都抛 ReferenceIntegrityError，
    不允许静默缺边。

    structural edge 类型映射：
    - Claim.claimant_ref → claimed_by (Claim → Person)
    - Event.participant_refs[*] → involved_in (Person → Event)
    - Claim.about_refs[*] → about (Claim → X)
    - Evidence.about_refs[*] → about (Evidence → X)
    - Event.location_ref → occurs_in (Event → Location)
    - Event.object_refs[*] → involves_object (Event → Object)
    """
    edges: list[GraphEdge] = []
    now = _now()

    def _add(source: str, target: str, edge_type: GraphEdgeType, label: str) -> None:
        edges.append(
            GraphEdge(
                id=f"edge-{uuid4().hex[:10]}",
                source=source,
                target=target,
                edge_type=edge_type,
                label=label,
                created_at=now,
            )
        )

    for claim in case.claims:
        if claim.claimant_ref is not None:
            _checked_ref(case, claim.claimant_ref, Person, f"claim:{claim.id}", "claimant_ref")
            _add(claim.id, claim.claimant_ref, GraphEdgeType.claimed_by, "claimed_by")
        for ref in claim.about_refs:
            if case.entity_by_id(ref) is None:
                raise ReferenceIntegrityError(
                    f"dangling ref: claim:{claim.id}:about_ref={ref}",
                    invalid_refs=[f"claim:{claim.id}:about_ref:{ref}"],
                )
            _add(claim.id, ref, GraphEdgeType.about, "about")

    for evidence in case.evidences:
        for ref in evidence.about_refs:
            if case.entity_by_id(ref) is None:
                raise ReferenceIntegrityError(
                    f"dangling ref: evidence:{evidence.id}:about_ref={ref}",
                    invalid_refs=[f"evidence:{evidence.id}:about_ref:{ref}"],
                )
            _add(evidence.id, ref, GraphEdgeType.about, "about")

    for event in case.events:
        if event.location_ref is not None:
            _checked_ref(case, event.location_ref, Location, f"event:{event.id}", "location_ref")
            _add(event.id, event.location_ref, GraphEdgeType.occurs_in, "occurs_in")
        for ref in event.object_refs:
            _checked_ref(case, ref, Object, f"event:{event.id}", "object_ref")
            _add(event.id, ref, GraphEdgeType.involves_object, "involves_object")
        for ref in event.participant_refs:
            _checked_ref(case, ref, Person, f"event:{event.id}", "participant_ref")
            _add(ref, event.id, GraphEdgeType.involved_in, "involved_in")

    return edges


def _semantic_edges_for(relations: list[SemanticRelation]) -> list[GraphEdge]:
    """把 semantic relations 投影为 semantic GraphEdge（supports/contradicts）。"""
    edges: list[GraphEdge] = []
    for rel in relations:
        edge_type = (
            GraphEdgeType.supports
            if rel.relation_type == SemanticRelationType.supports
            else GraphEdgeType.contradicts
        )
        edges.append(
            GraphEdge(
                id=f"edge-{uuid4().hex[:10]}",
                source=rel.source_id,
                target=rel.target_id,
                edge_type=edge_type,
                label=rel.relation_type.value,
                created_at=rel.created_at,
            )
        )
    return edges


def build_semantic_edges(case: CaseData) -> list[GraphEdge]:
    """从 case.semantic_relations 投影 semantic GraphEdge。"""
    return _semantic_edges_for(case.semantic_relations)


def build_graph(case: CaseData) -> list[GraphEdge]:
    """合并 structural + semantic projection（runtime，不持久化）。"""
    return [*build_structural_edges(case), *build_semantic_edges(case)]


def _build_semantic_relations(case: CaseData, raw: list) -> list[SemanticRelation]:
    """把 LLM raw semantic_relations 校验/归一化为 SemanticRelation。

    端点约束（第一版）：
    - supports：source ∈ {Claim, Evidence}，target = Claim。
    - contradicts：source = Claim，target = Claim（对称 canonical dedup）。
    - source_id / target_id 必须存在于 case（entity_by_id）。
    违反约束的单条 relation 丢弃（记录 warning），不阻塞整体 success；
    这不同于 parser failure（parser failure 整体抛 error）。
    """
    result: list[SemanticRelation] = []
    seen_keys: set[tuple[str, str, str]] = set()
    now = _now()

    for item in raw:
        if not isinstance(item, dict):
            continue
        source_id = _normalize_optional_text(item.get("source_id"))
        target_id = _normalize_optional_text(item.get("target_id"))
        relation_raw = (_normalize_optional_text(item.get("relation_type")) or "").lower()
        if relation_raw not in {"supports", "contradicts"}:
            logger.warning("relation dropped: invalid relation_type=%r", relation_raw)
            continue
        if not source_id or not target_id:
            logger.warning("relation dropped: missing source_id/target_id")
            continue
        source = case.entity_by_id(source_id)
        target = case.entity_by_id(target_id)
        if source is None or target is None:
            logger.warning("relation dropped: dangling ref source=%s target=%s", source_id, target_id)
            continue

        relation_type = SemanticRelationType(relation_raw)
        if relation_type == SemanticRelationType.supports:
            if not isinstance(source, (Claim, Evidence)) or not isinstance(target, Claim):
                logger.warning("relation dropped: supports endpoint violation source=%s target=%s", source_id, target_id)
                continue
        else:  # contradicts
            if not isinstance(source, Claim) or not isinstance(target, Claim):
                logger.warning("relation dropped: contradicts endpoint violation source=%s target=%s", source_id, target_id)
                continue

        # contradicts 对称 canonical dedup（无向去重）；supports 有向去重。
        if relation_type == SemanticRelationType.contradicts:
            key = ("contradicts", *sorted((source_id, target_id)))
        else:
            key = ("supports", source_id, target_id)
        if key in seen_keys:
            continue
        seen_keys.add(key)

        confidence_raw = (
            _normalize_optional_text(item.get("detection_confidence"))
            or _normalize_optional_text(item.get("confidence"))
        )
        confidence = _confidence(confidence_raw)
        result.append(
            SemanticRelation(
                id=f"rel-{uuid4().hex[:10]}",
                source_id=source_id,
                target_id=target_id,
                relation_type=relation_type,
                detection_confidence=confidence,
                rationale=str(item.get("rationale", "")).strip() or "",
                created_at=now,
            )
        )

    return result


async def run_reasoning_agent(case: CaseData, req: ReasoningRequest) -> ReasoningResponse:
    """Phase 1c：Reasoning stage 被显式禁用。

    Phase 1b 的 reasoning 依赖 legacy graph（case.edges / legacy EdgeType /
    RelationRecord），Phase 1c 已 clean-break 删除这些 graph persistence。
    旧 reasoning 功能本就 not functionally verified；本 Phase 不重新设计 reasoning，
    只做 clean break + 显式 disabled boundary。调用方（main.py）映射为 501。
    """
    _append_agent_run(
        case,
        RouterTask.reasoning,
        "generate_reasoning_view",
        req.model_dump(),
        {"status": "disabled", "phase": "phase1c"},
        status="fallback",
        error="ReasoningStageDisabledError: reasoning stage awaiting redesign (Phase 1c clean-break)",
    )
    append_case_log(
        case.case_id,
        "llm",
        {
            "stage": "reasoning",
            "status": "disabled",
            "message": "reasoning stage disabled in Phase 1c; awaiting redesign after legacy graph clean-break",
        },
    )
    raise ReasoningStageDisabledError()


async def run_qa_agent(case: CaseData, req: QARequest) -> QAResponse:
    q = req.question.strip().lower()
    matched: list[str] = []

    def _entity_text(entity) -> str:
        if isinstance(entity, (Person, Location, Object)):
            return f"{entity.name} {entity.description or ''}"
        if isinstance(entity, Event):
            return f"{entity.description} {entity.evidence_quote}"
        if isinstance(entity, Claim):
            return f"{entity.proposition} {entity.evidence_quote}"
        if isinstance(entity, Evidence):
            return f"{entity.description} {entity.evidence_quote}"
        return entity.id

    for entity in case.all_entities():
        hay = _entity_text(entity).lower()
        if q and q in hay:
            matched.append(entity.id)

    all_entities = case.all_entities()
    if not matched and all_entities:
        matched = [all_entities[0].id]

    answer = "No matched information found."
    if matched:
        first = case.entity_by_id(matched[0])
        if first:
            first_text = _entity_text(first)
            answer = f"Matched {len(matched)} item(s). Primary match: {first_text[:120]}"

    response = QAResponse(
        answer=answer,
        matched_items=matched,
        highlight_targets=[f"meta-{mid}" for mid in matched],
        confidence=ConfidenceLevel.medium,
    )
    _append_agent_run(case, RouterTask.qa, "answer", req.model_dump(), response.model_dump(mode="json"))
    return response


async def run_interaction_agent(case: CaseData, req: InteractionRequest) -> InteractionResponse:
    text = req.user_input.strip().lower()
    intent = IntentType.qa
    mode = ResponseMode.text
    actions: list[UIAction] = []
    message = "Processed interaction request."

    if any(k in text for k in ["highlight", "高亮"]):
        intent = IntentType.search
        mode = ResponseMode.mixed
        actions.append(UIAction(action=UIActionType.highlight, targets=req.current_selection, params={}))
        message = "Highlighted selected targets."
    elif any(k in text for k in ["switch", "view", "切换", "视图"]):
        intent = IntentType.view_switch
        mode = ResponseMode.canvas
        actions.append(UIAction(action=UIActionType.open_view, targets=[], params={"view": "reasoning"}))
        message = "Requested view switch."
    elif any(k in text for k in ["rearrange", "重排", "整理"]):
        intent = IntentType.canvas_edit
        mode = ResponseMode.canvas
        actions.append(UIAction(action=UIActionType.rearrange, targets=req.current_selection, params={"layout": "auto"}))
        message = "Requested canvas rearrangement."
    elif any(k in text for k in ["create", "创建", "新建"]):
        intent = IntentType.canvas_edit
        mode = ResponseMode.canvas
        actions.append(UIAction(action=UIActionType.create_node, targets=[], params={"node_type": "inference"}))
        message = "Requested node creation."

    response = InteractionResponse(intent_type=intent, response_mode=mode, ui_actions=actions, assistant_message=message)
    _append_agent_run(case, RouterTask.interaction, "act", req.model_dump(), response.model_dump(mode="json"))
    return response




async def track_interaction_event(case: CaseData, req: InteractionTrackRequest) -> str:
    run_id = f"run-{uuid4().hex[:10]}"
    now = _now()
    append_case_log(
        case.case_id,
        "interaction",
        {
            "run_id": run_id,
            "action": req.action,
            "targets": req.targets,
            "params": req.params,
            "context": req.context,
            "status": "success",
            "timestamp": now.isoformat(),
        },
    )
    case.updated_at = now
    return run_id
























