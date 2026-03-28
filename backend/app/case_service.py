"""Case service helpers for file ingestion and graph updates."""

from __future__ import annotations

import asyncio
import json
import os
from io import BytesIO
from datetime import datetime
from pathlib import Path
from typing import Literal, Sequence, cast
from urllib import error as urllib_error
from urllib import request as urllib_request
from uuid import uuid4

from docx import Document
from fastapi import UploadFile
from pypdf import PdfReader
from starlette.datastructures import UploadFile as StarletteUploadFile

from app.case_store import append_case_log, case_upload_dir
from app.schemas import (
    AgentRunLog,
    CaseData,
    CaseFile,
    CardPosition,
    CertaintyLevel,
    ConfidenceLevel,
    DisplayLevel,
    EdgeType,
    ExtractionRequest,
    FileParseStatus,
    FileType,
    GenerateInferenceRequest,
    GraphEdge,
    InfoUnit,
    InfoUnitType,
    InferenceCard,
    InferenceDetail,
    InferenceType,
    IntentType,
    InteractionRequest,
    InteractionResponse,
    InteractionTrackRequest,
    MetaCard,
    MetaDetail,
    MetaType,
    QARequest,
    QAResponse,
    ReasoningRequest,
    ReasoningResponse,
    RecommendedView,
    RelationRecord,
    RelationRequest,
    ResponseMode,
    RouteRequest,
    RouteResponse,
    RouterTask,
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


_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp"}
PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
PROMPT_FILE_BY_TASK = {
    "router": "router_system_prompt.md",
    "extraction": "extraction_system_prompt.md",
    "relation": "relation_system_prompt.md",
    "reasoning": "reasoning_system_prompt.md",
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


def _map_info_type_to_meta_type(info_type: InfoUnitType) -> MetaType:
    if info_type == InfoUnitType.person:
        return MetaType.person
    if info_type == InfoUnitType.event:
        return MetaType.event
    if info_type == InfoUnitType.claim:
        return MetaType.claim
    if info_type == InfoUnitType.time:
        return MetaType.time
    if info_type == InfoUnitType.location:
        return MetaType.location
    return MetaType.object

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
def _create_meta_card(title: str, summary: str, file_id: str) -> MetaCard:
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
        meta_type=MetaType.document,
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
            notes="Auto-generated",
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
        edges=[],
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

        case.meta_cards.append(
            _create_meta_card(
                title=filename,
                summary=_preview_text(content, 96) or "No extracted text.",
                file_id=file_id,
            )
        )

    case.updated_at = _now()
    await run_extraction_agent(case, ExtractionRequest(case_id=case.case_id, file_ids=[f.file_id for f in case.files]))
    return case



async def append_files_to_case(
    case: CaseData,
    files: Sequence[UploadFile | StarletteUploadFile],
) -> CaseData:
    """Append uploaded files to an existing case."""
    # NOTE: 暂不重跑 extraction agent，待补充逻辑
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


def generate_inference(case: CaseData, request: GenerateInferenceRequest) -> tuple[list[InferenceCard], list[GraphEdge], WorkspaceState]:
    selected = request.selected_card_ids
    claim = (request.user_prompt or "").strip() or f"{request.mode} based on {len(selected)} selected card(s)."

    new_card = _create_inference_card(claim=claim, selected_ids=selected, mode=request.mode)
    new_edges: list[GraphEdge] = []

    for source_id in selected:
        new_edges.append(
            GraphEdge(
                id=f"edge-{uuid4().hex[:10]}",
                source=source_id,
                target=new_card.id,
                edge_type=EdgeType.supports,
                label="generated",
                created_at=_now(),
            )
        )

    workspace = case.workspace_state.model_copy(deep=True)
    workspace.focused_card_id = new_card.id
    workspace.selected_card_ids = [new_card.id]

    return [new_card], new_edges, workspace


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


def _provider_config() -> tuple[str, str, str, str]:
    _load_local_env()
    api_key = (
        os.getenv("GITHUB_TOKEN", "").strip()
        or os.getenv("GITHUB_MODELS_TOKEN", "").strip()
        or os.getenv("OPENAI_API_KEY", "").strip()
    )
    base_url = (
        os.getenv("GITHUB_ENDPOINT", "").strip()
        or os.getenv("OPENAI_BASE_URL", "").strip()
        or "https://models.github.ai/inference"
    ).rstrip("/")
    model = (
        os.getenv("GITHUB_MODEL_ID", "").strip()
        or os.getenv("OPENAI_MODEL", "").strip()
        or "openai/gpt-4.1-mini"
    )
    api_version = os.getenv("GITHUB_API_VERSION", "2022-11-28")
    return api_key, base_url, model, api_version


def _extract_json(text: str) -> dict:
    content = (text or "").strip()
    if "```json" in content:
        content = content.split("```json", 1)[1]
    if "```" in content:
        content = content.split("```", 1)[0]
    content = content.strip()
    return json.loads(content)


def _build_inference_prompt(case: CaseData, request: GenerateInferenceRequest) -> str:
    selected = request.selected_card_ids
    selected_meta = [card for card in case.meta_cards if card.id in selected]
    selected_infer = [card for card in case.inference_cards if card.id in selected]

    return (
        f"Case title:\n{case.case_title}\n\n"
        f"Mode: {request.mode}\n"
        f"User prompt: {(request.user_prompt or '').strip() or '[empty]'}\n\n"
        f"Selected meta cards:\n{json.dumps([c.model_dump(mode='json') for c in selected_meta], ensure_ascii=False, indent=2)}\n\n"
        f"Selected inference cards:\n{json.dumps([c.model_dump(mode='json') for c in selected_infer], ensure_ascii=False, indent=2)}\n\n"
        "Return strict JSON with keys: new_inferences, new_edges.\n"
        "new_inferences: list of {title, claim, summary, inference_type, supporting_card_ids}.\n"
        "inference_type must be one of: hypothesis, conclusion, conflict, missing_evidence, reasoning_step, evidence_chain, risk, other.\n"
        "new_edges: list of {source, target_ref, edge_type, label}.\n"
        "target_ref is index notation like 'inference_0' pointing to new_inferences item.\n"
        "edge_type must be one of: relates_to,supports,opposes,derives,mentions,conflicts_with,missing_for,cites,belongs_to.\n"
        "No markdown, no extra text."
    )


def _call_llm_sync(system_prompt: str, user_prompt: str) -> dict:
    api_key, base_url, model, api_version = _provider_config()
    if not api_key:
        raise LLMRequestError("No API token configured")

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.2,
    }

    data = json.dumps(payload).encode("utf-8")
    req = urllib_request.Request(
        url=f"{base_url}/chat/completions",
        data=data,
        method="POST",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": api_version,
        },
    )

    http_status: int | None = None
    provider_body_text: str = ""
    try:
        with urllib_request.urlopen(req, timeout=30) as resp:
            http_status = int(getattr(resp, "status", 200))
            provider_body_text = resp.read().decode("utf-8")
    except urllib_error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore") if exc.fp else str(exc)
        raise LLMRequestError(
            message=f"LLM HTTP {exc.code}",
            status_code=exc.code,
            provider_body=detail,
        ) from exc
    except Exception as exc:  # pragma: no cover
        raise LLMRequestError(message=f"LLM request failed: {exc}") from exc

    parsed = json.loads(provider_body_text)
    content = parsed.get("choices", [{}])[0].get("message", {}).get("content", "")
    if isinstance(content, list):
        text_parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text_parts.append(str(item.get("text", "")))
        content = "\n".join(text_parts)

    if not isinstance(content, str):
        content = str(content)

    return {
        "parsed_json": _extract_json(content),
        "raw_reply": content,
        "request_meta": {
            "temperature": payload["temperature"],
            "model": model,
            "base_url": base_url,
        },
        "response_meta": {
            "http_status": http_status,
            "provider_body_preview": provider_body_text[:4000],
        },
    }


async def generate_inference_with_llm(
    case: CaseData,
    request: GenerateInferenceRequest,
) -> tuple[list[InferenceCard], list[GraphEdge], WorkspaceState, str]:
    fallback_cards, fallback_edges, fallback_workspace = generate_inference(case, request)

    system_prompt = _get_system_prompt("reasoning")
    user_prompt = _build_inference_prompt(case, request)
    selected = request.selected_card_ids
    selected_meta = [card for card in case.meta_cards if card.id in selected]
    selected_infer = [card for card in case.inference_cards if card.id in selected]
    sent_sections = [
        "case_title",
        "mode",
        "user_prompt",
        "selected_meta_cards",
        "selected_inference_cards",
    ]
    request_snapshot = {
        "mode": request.mode,
        "user_prompt": request.user_prompt or "",
        "selected_meta_cards": [c.model_dump(mode="json") for c in selected_meta],
        "selected_inference_cards": [c.model_dump(mode="json") for c in selected_infer],
    }

    try:
        llm_result = await asyncio.to_thread(_call_llm_sync, system_prompt, user_prompt)
        parsed = llm_result.get("parsed_json", {})
        raw_reply = str(llm_result.get("raw_reply", ""))
        request_meta = llm_result.get("request_meta", {})
        response_meta = llm_result.get("response_meta", {})
        raw_inferences = parsed.get("new_inferences", []) if isinstance(parsed, dict) else []
        raw_edges = parsed.get("new_edges", []) if isinstance(parsed, dict) else []

        if not isinstance(raw_inferences, list) or not raw_inferences:
            append_case_log(
                case.case_id,
                "llm",
                {
                    "stage": "reasoning",
                    "mode": request.mode,
                    "status": "empty",
                    "message": "llm-empty-inference-fallback",
                    "fallback_reason": "LLM returned empty new_inferences",
                    "error_code": response_meta.get("http_status"),
                    "sent_sections": sent_sections,
                    "request_meta": request_meta,
                    "request_snapshot": request_snapshot,
                    "raw_reply": raw_reply,
                    "parsed_snapshot": parsed,
                    "response_meta": response_meta,
                },
            )
            return fallback_cards, fallback_edges, fallback_workspace, "llm-empty-inference-fallback"

        created_cards: list[InferenceCard] = []
        for item in raw_inferences[:6]:
            if not isinstance(item, dict):
                continue
            claim = str(item.get("claim", "")).strip() or str(item.get("summary", "")).strip()
            if not claim:
                continue
            title = str(item.get("title", "")).strip() or None
            supporting = item.get("supporting_card_ids", request.selected_card_ids)
            if not isinstance(supporting, list):
                supporting = request.selected_card_ids
            mode = str(item.get("inference_type", request.mode))
            card = _create_inference_card(claim=claim, selected_ids=[str(i) for i in supporting], mode=mode, title=title)
            summary = str(item.get("summary", "")).strip()
            if summary:
                card.summary = summary
            created_cards.append(card)

        if not created_cards:
            append_case_log(
                case.case_id,
                "llm",
                {
                    "stage": "reasoning",
                    "mode": request.mode,
                    "status": "invalid",
                    "message": "llm-invalid-inference-fallback",
                    "fallback_reason": "LLM returned invalid new_inferences",
                    "error_code": response_meta.get("http_status"),
                    "sent_sections": sent_sections,
                    "request_meta": request_meta,
                    "request_snapshot": request_snapshot,
                    "raw_reply": raw_reply,
                    "parsed_snapshot": parsed,
                    "response_meta": response_meta,
                },
            )
            return fallback_cards, fallback_edges, fallback_workspace, "llm-invalid-inference-fallback"

        ref_map = {f"inference_{idx}": card.id for idx, card in enumerate(created_cards)}
        created_edges: list[GraphEdge] = []

        if isinstance(raw_edges, list):
            for item in raw_edges[:40]:
                if not isinstance(item, dict):
                    continue
                source = str(item.get("source", "")).strip()
                target_ref = str(item.get("target_ref", "")).strip()
                target = ref_map.get(target_ref)
                if not source or not target:
                    continue
                edge_type_raw = str(item.get("edge_type", "supports")).strip()
                try:
                    edge_type = EdgeType(edge_type_raw)
                except Exception:
                    edge_type = EdgeType.supports
                label = str(item.get("label", "")).strip() or "generated"
                created_edges.append(
                    GraphEdge(
                        id=f"edge-{uuid4().hex[:10]}",
                        source=source,
                        target=target,
                        edge_type=edge_type,
                        label=label,
                        created_at=_now(),
                    )
                )

        if not created_edges:
            for card in created_cards:
                for source_id in request.selected_card_ids:
                    created_edges.append(
                        GraphEdge(
                            id=f"edge-{uuid4().hex[:10]}",
                            source=source_id,
                            target=card.id,
                            edge_type=EdgeType.supports,
                            label="generated",
                            created_at=_now(),
                        )
                    )

        workspace = case.workspace_state.model_copy(deep=True)
        workspace.focused_card_id = created_cards[0].id
        workspace.selected_card_ids = [created_cards[0].id]

        append_case_log(
            case.case_id,
            "llm",
            {
                "stage": "reasoning",
                "mode": request.mode,
                "status": "success",
                "message": "llm-success",
                "fallback_reason": "",
                "error_code": response_meta.get("http_status"),
                "sent_sections": sent_sections,
                "request_meta": request_meta,
                "request_snapshot": request_snapshot,
                "raw_reply": raw_reply,
                "parsed_snapshot": parsed,
                "response_meta": response_meta,
                "new_inference_count": len(created_cards),
                "new_edge_count": len(created_edges),
            },
        )
        return created_cards, created_edges, workspace, "llm-success"
    except LLMRequestError as exc:
        append_case_log(
            case.case_id,
            "llm",
            {
                "stage": "reasoning",
                "mode": request.mode,
                "status": "fallback",
                "message": f"llm-fallback: {exc}",
                "fallback_reason": str(exc),
                "error_code": exc.status_code,
                "sent_sections": sent_sections,
                "request_snapshot": request_snapshot,
                "raw_reply": "",
                "parsed_snapshot": {},
                "response_meta": {
                    "http_status": exc.status_code,
                    "provider_body_preview": (exc.provider_body or "")[:4000],
                },
            },
        )
        return fallback_cards, fallback_edges, fallback_workspace, f"llm-fallback: {exc}"
    except Exception as exc:
        append_case_log(
            case.case_id,
            "llm",
            {
                "stage": "reasoning",
                "mode": request.mode,
                "status": "fallback",
                "message": f"llm-fallback: {exc}",
                "fallback_reason": f"Unexpected error: {exc}",
                "error_code": None,
                "sent_sections": sent_sections,
                "request_snapshot": request_snapshot,
                "raw_reply": "",
                "parsed_snapshot": {},
            },
        )
        return fallback_cards, fallback_edges, fallback_workspace, f"llm-fallback: {exc}"






async def route_task(case: CaseData, req: RouteRequest) -> RouteResponse:
    text = req.user_input.strip().lower()
    task = RouterTask.qa
    subtask = "answer"
    requires_tool = False

    if any(k in text for k in ["upload", "parse", "extract", "上传", "抽取"]):
        task = RouterTask.extraction
        subtask = "extract_info_units"
        requires_tool = True
    elif any(k in text for k in ["relation", "关系"]):
        task = RouterTask.relation
        subtask = "build_relations"
        requires_tool = True
    elif any(k in text for k in ["reason", "hypothesis", "推理", "结论", "冲突"]):
        task = RouterTask.reasoning
        subtask = "generate_reasoning"
        requires_tool = True
    elif any(k in text for k in ["highlight", "open", "switch", "rearrange", "高亮", "切换", "重排"]):
        task = RouterTask.interaction
        subtask = "ui_action"

    response = RouteResponse(task=task, subtask=subtask, target_scope=req.current_selection, requires_tool=requires_tool)
    _append_agent_run(case, RouterTask.interaction, "router", req.model_dump(), response.model_dump(mode="json"))
    return response



def _normalize_confidence(value: str | None) -> ConfidenceLevel:
    raw = (value or "").strip().lower()
    if raw == "high":
        return ConfidenceLevel.high
    if raw == "low":
        return ConfidenceLevel.low
    return ConfidenceLevel.medium


def _normalize_info_type(value: str | None) -> InfoUnitType:
    raw = (value or "").strip().lower()
    try:
        return InfoUnitType(raw)
    except Exception:
        return InfoUnitType.claim


def _normalize_certainty(value: str | None) -> CertaintyLevel:
    raw = (value or "").strip().lower()
    if raw == "inferred":
        return CertaintyLevel.inferred
    return CertaintyLevel.explicit


def _relation_type_to_edge_type(relation_type: str) -> EdgeType:
    raw = (relation_type or "").strip().lower()
    if raw in {"support", "supports"}:
        return EdgeType.supports
    if raw in {"contradict", "contradicts", "conflict", "conflicts_with"}:
        return EdgeType.conflicts_with
    if raw in {"belong_to", "belongs_to"}:
        return EdgeType.belongs_to
    if raw in {"refer_to", "mentions", "mention"}:
        return EdgeType.mentions
    return EdgeType.relates_to


def _fallback_extract(case: CaseData, req: ExtractionRequest) -> list[InfoUnit]:
    file_ids = req.file_ids or [f.file_id for f in case.files]
    target_files = [f for f in case.files if f.file_id in file_ids]

    units: list[InfoUnit] = []
    for file_item in target_files:
        text = (file_item.preview_text or file_item.filename).strip()
        now = _now()
        units.append(
            InfoUnit(
                id=f"info-{uuid4().hex[:10]}",
                type=InfoUnitType.claim if text else InfoUnitType.object,
                title=file_item.filename,
                summary=(file_item.preview_text or file_item.filename)[:160],
                detail=text[:1200],
                source_refs=[file_item.file_id],
                confidence=ConfidenceLevel.medium,
                extraction_reason="Derived from uploaded file preview and metadata.",
                evidence_quote=text[:260],
                created_at=now,
                updated_at=now,
            )
        )

    if req.raw_text:
        now = _now()
        units.append(
            InfoUnit(
                id=f"info-{uuid4().hex[:10]}",
                type=InfoUnitType.claim,
                title="User Input",
                summary=req.raw_text[:160],
                detail=req.raw_text[:1200],
                source_refs=file_ids,
                confidence=ConfidenceLevel.medium,
                extraction_reason="Directly extracted from raw_text input.",
                evidence_quote=req.raw_text[:260],
                created_at=now,
                updated_at=now,
            )
        )

    return units


def _sync_meta_cards_from_info_units(case: CaseData, units: list[InfoUnit]) -> None:
    synced_cards: list[MetaCard] = []
    for i, info in enumerate(units):
        now = _now()
        synced_cards.append(
            MetaCard(
                id=f"meta-{info.id}",
                title=info.title,
                summary=info.summary,
                display_level=DisplayLevel.level_1,
                position=CardPosition(x=80 + (i % 4) * 260, y=100 + (i // 4) * 170),
                source_file_ids=info.source_refs,
                        created_at=now,
                updated_at=now,
                meta_type=_map_info_type_to_meta_type(info.type),
                detail=MetaDetail(
                    name=info.title,
                    description=info.detail,
                    tags=[f"confidence:{info.confidence.value}", "extracted"],
                    attributes={
                        "extraction_reason": info.extraction_reason,
                        "evidence_quote": info.evidence_quote,
                    },
                ),
            )
        )
    case.meta_cards = synced_cards


async def run_extraction_agent(case: CaseData, req: ExtractionRequest) -> list[InfoUnit]:
    file_ids = req.file_ids or [f.file_id for f in case.files]
    target_files = [f for f in case.files if f.file_id in file_ids]

    system_prompt = _get_system_prompt("extraction")
    request_snapshot = {
        "case_title": case.case_title,
        "file_ids": file_ids,
        "context": req.context or "",
        "raw_text": req.raw_text or "",
        "files": [
            {
                "file_id": f.file_id,
                "filename": f.filename,
                "file_type": f.file_type.value,
                "parse_status": f.parse_status.value,
                "content": _file_content_for_extraction(case, f),
            }
            for f in target_files
        ],
    }
    sent_sections = ["case_title", "file_ids", "context", "raw_text", "files"]
    user_prompt = (
        "Input payload:\n"
        + json.dumps(request_snapshot, ensure_ascii=False, indent=2)
        + "\n\nReturn strict JSON with info_units only."
    )

    units: list[InfoUnit] = []
    try:
        llm_result = await asyncio.to_thread(_call_llm_sync, system_prompt, user_prompt)
        parsed = llm_result.get("parsed_json", {})
        raw_reply = str(llm_result.get("raw_reply", ""))
        request_meta = llm_result.get("request_meta", {})
        response_meta = llm_result.get("response_meta", {})
        raw_units = parsed.get("info_units", []) if isinstance(parsed, dict) else []

        if not isinstance(raw_units, list) or not raw_units:
            units = _fallback_extract(case, req)
            status = "empty"
            message = "llm-empty-extraction-fallback"
            fallback_reason = "LLM returned empty info_units"
        else:
            for item in raw_units[:200]:
                if not isinstance(item, dict):
                    continue
                now = _now()
                unit_id = str(item.get("id", "")).strip() or f"info-{uuid4().hex[:10]}"
                title = str(item.get("title", "")).strip() or "Untitled"
                summary = str(item.get("summary", "")).strip() or title
                detail = str(item.get("detail", "")).strip() or summary
                extraction_reason = str(item.get("extraction_reason", "")).strip() or "LLM extracted this as an independent info unit."
                evidence_quote = str(item.get("evidence_quote", "")).strip() or detail[:260]
                source_refs_raw = item.get("source_refs", [])
                if isinstance(source_refs_raw, list):
                    source_refs = [str(v) for v in source_refs_raw if str(v).strip()]
                else:
                    source_refs = []
                if not source_refs:
                    source_refs = file_ids

                units.append(
                    InfoUnit(
                        id=unit_id,
                        type=_normalize_info_type(str(item.get("type", ""))),
                        title=title,
                        summary=summary[:300],
                        detail=detail[:2000],
                        source_refs=source_refs,
                        confidence=_normalize_confidence(str(item.get("confidence", ""))),
                        extraction_reason=extraction_reason[:500],
                        evidence_quote=evidence_quote[:500],
                        created_at=now,
                        updated_at=now,
                    )
                )

            if not units:
                units = _fallback_extract(case, req)
                status = "invalid"
                message = "llm-invalid-extraction-fallback"
                fallback_reason = "LLM returned invalid info_units"
            else:
                status = "success"
                message = "llm-extraction-success"
                fallback_reason = ""

        case.info_units = units
        _sync_meta_cards_from_info_units(case, units)
        case.updated_at = _now()

        append_case_log(
            case.case_id,
            "llm",
            {
                "stage": "extraction",
                "status": status,
                "message": message,
                "fallback_reason": fallback_reason,
                "error_code": response_meta.get("http_status"),
                "sent_sections": sent_sections,
                "request_meta": request_meta,
                "request_snapshot": request_snapshot,
                "raw_reply": raw_reply,
                "parsed_snapshot": parsed,
                "response_meta": response_meta,
                "info_unit_count": len(units),
            },
        )
    except LLMRequestError as exc:
        units = _fallback_extract(case, req)
        case.info_units = units
        _sync_meta_cards_from_info_units(case, units)
        case.updated_at = _now()
        append_case_log(
            case.case_id,
            "llm",
            {
                "stage": "extraction",
                "status": "fallback",
                "message": f"llm-fallback: {exc}",
                "fallback_reason": str(exc),
                "error_code": exc.status_code,
                "sent_sections": sent_sections,
                "request_snapshot": request_snapshot,
                "raw_reply": "",
                "parsed_snapshot": {},
                "response_meta": {
                    "http_status": exc.status_code,
                    "provider_body_preview": (exc.provider_body or "")[:4000],
                },
                "info_unit_count": len(units),
            },
        )
    except Exception as exc:
        units = _fallback_extract(case, req)
        case.info_units = units
        _sync_meta_cards_from_info_units(case, units)
        case.updated_at = _now()
        append_case_log(
            case.case_id,
            "llm",
            {
                "stage": "extraction",
                "status": "fallback",
                "message": f"llm-fallback: {exc}",
                "fallback_reason": f"Unexpected error: {exc}",
                "error_code": None,
                "sent_sections": sent_sections,
                "request_snapshot": request_snapshot,
                "raw_reply": "",
                "parsed_snapshot": {},
                "info_unit_count": len(units),
            },
        )

    _append_agent_run(case, RouterTask.extraction, "extract_info_units", req.model_dump(), {"count": len(case.info_units)})
    return case.info_units


async def run_relation_agent(case: CaseData, req: RelationRequest) -> tuple[list[RelationRecord], list[GraphEdge]]:
    ids = set(req.info_unit_ids or [item.id for item in case.info_units])
    nodes = [item for item in case.info_units if item.id in ids]

    system_prompt = _get_system_prompt("relation")
    request_snapshot = {
        "case_title": case.case_title,
        "info_unit_ids": [n.id for n in nodes],
        "info_units": [
            {
                "id": n.id,
                "type": n.type.value,
                "title": n.title,
                "summary": n.summary,
                "detail": n.detail[:1200],
                "source_refs": n.source_refs,
                "evidence_quote": n.evidence_quote,
            }
            for n in nodes
        ],
    }
    sent_sections = ["case_title", "info_unit_ids", "info_units"]
    user_prompt = (
        "Input payload:\n"
        + json.dumps(request_snapshot, ensure_ascii=False, indent=2)
        + "\n\nReturn strict JSON with key relations only."
    )

    relations: list[RelationRecord] = []
    edges: list[GraphEdge] = []

    def _fallback_relation() -> tuple[list[RelationRecord], list[GraphEdge]]:
        fallback_relations: list[RelationRecord] = []
        fallback_edges: list[GraphEdge] = []
        for idx in range(len(nodes) - 1):
            src = nodes[idx]
            tgt = nodes[idx + 1]
            rel = RelationRecord(
                id=f"rel-{uuid4().hex[:10]}",
                source_id=src.id,
                target_id=tgt.id,
                relation_type="co_occurs",
                confidence=ConfidenceLevel.medium,
                evidence_basis="Shared adjacency in extracted sequence.",
                rationale="Conservative baseline relation generated from extraction order.",
                certainty_level=CertaintyLevel.inferred,
                created_at=_now(),
            )
            fallback_relations.append(rel)
            fallback_edges.append(
                GraphEdge(
                    id=f"edge-{uuid4().hex[:10]}",
                    source=f"meta-{src.id}",
                    target=f"meta-{tgt.id}",
                    edge_type=EdgeType.relates_to,
                    label="co_occurs",
                    created_at=_now(),
                )
            )
        return fallback_relations, fallback_edges

    try:
        llm_result = await asyncio.to_thread(_call_llm_sync, system_prompt, user_prompt)
        parsed = llm_result.get("parsed_json", {})
        raw_reply = str(llm_result.get("raw_reply", ""))
        request_meta = llm_result.get("request_meta", {})
        response_meta = llm_result.get("response_meta", {})
        raw_relations = parsed.get("relations", []) if isinstance(parsed, dict) else []

        if not isinstance(raw_relations, list) or not raw_relations:
            relations, edges = _fallback_relation()
            status = "empty"
            message = "llm-empty-relation-fallback"
            fallback_reason = "LLM returned empty relations"
        else:
            valid_ids = {n.id for n in nodes}
            for item in raw_relations[:400]:
                if not isinstance(item, dict):
                    continue
                source_id = str(item.get("source_id", "")).strip()
                target_id = str(item.get("target_id", "")).strip()
                if source_id not in valid_ids or target_id not in valid_ids or source_id == target_id:
                    continue
                relation_type = str(item.get("relation_type", "relates_to")).strip() or "relates_to"
                rel = RelationRecord(
                    id=str(item.get("id", "")).strip() or f"rel-{uuid4().hex[:10]}",
                    source_id=source_id,
                    target_id=target_id,
                    relation_type=relation_type,
                    confidence=_normalize_confidence(str(item.get("confidence", ""))),
                    evidence_basis=str(item.get("evidence_basis", "")).strip() or "",
                    rationale=str(item.get("rationale", "")).strip() or "",
                    certainty_level=_normalize_certainty(str(item.get("certainty_level", ""))),
                    created_at=_now(),
                )
                relations.append(rel)
                edges.append(
                    GraphEdge(
                        id=f"edge-{uuid4().hex[:10]}",
                        source=f"meta-{source_id}",
                        target=f"meta-{target_id}",
                        edge_type=_relation_type_to_edge_type(relation_type),
                        label=relation_type,
                        created_at=_now(),
                    )
                )

            if not relations:
                relations, edges = _fallback_relation()
                status = "invalid"
                message = "llm-invalid-relation-fallback"
                fallback_reason = "LLM returned invalid relations"
            else:
                status = "success"
                message = "llm-relation-success"
                fallback_reason = ""

        case.relations = relations
        case.edges = [edge for edge in case.edges if edge.edge_type != EdgeType.relates_to] + edges
        case.updated_at = _now()

        append_case_log(
            case.case_id,
            "llm",
            {
                "stage": "relation",
                "status": status,
                "message": message,
                "fallback_reason": fallback_reason,
                "error_code": response_meta.get("http_status"),
                "sent_sections": sent_sections,
                "request_meta": request_meta,
                "request_snapshot": request_snapshot,
                "raw_reply": raw_reply,
                "parsed_snapshot": parsed,
                "response_meta": response_meta,
                "relation_count": len(relations),
                "edge_count": len(edges),
            },
        )
    except LLMRequestError as exc:
        relations, edges = _fallback_relation()
        case.relations = relations
        case.edges = [edge for edge in case.edges if edge.edge_type != EdgeType.relates_to] + edges
        case.updated_at = _now()
        append_case_log(
            case.case_id,
            "llm",
            {
                "stage": "relation",
                "status": "fallback",
                "message": f"llm-fallback: {exc}",
                "fallback_reason": str(exc),
                "error_code": exc.status_code,
                "sent_sections": sent_sections,
                "request_snapshot": request_snapshot,
                "raw_reply": "",
                "parsed_snapshot": {},
                "response_meta": {
                    "http_status": exc.status_code,
                    "provider_body_preview": (exc.provider_body or "")[:4000],
                },
                "relation_count": len(relations),
                "edge_count": len(edges),
            },
        )
    except Exception as exc:
        relations, edges = _fallback_relation()
        case.relations = relations
        case.edges = [edge for edge in case.edges if edge.edge_type != EdgeType.relates_to] + edges
        case.updated_at = _now()
        append_case_log(
            case.case_id,
            "llm",
            {
                "stage": "relation",
                "status": "fallback",
                "message": f"llm-fallback: {exc}",
                "fallback_reason": f"Unexpected error: {exc}",
                "error_code": None,
                "sent_sections": sent_sections,
                "request_snapshot": request_snapshot,
                "raw_reply": "",
                "parsed_snapshot": {},
                "relation_count": len(relations),
                "edge_count": len(edges),
            },
        )

    _append_agent_run(case, RouterTask.relation, "build_relations", req.model_dump(), {"relations": len(relations), "edges": len(edges)})
    return relations, edges


async def run_reasoning_agent(case: CaseData, req: ReasoningRequest) -> ReasoningResponse:
    selected_info = req.selected_info_units
    selected_meta_ids = [f"meta-{sid}" for sid in selected_info if any(m.id == f"meta-{sid}" for m in case.meta_cards)]
    if not selected_meta_ids:
        selected_meta_ids = case.workspace_state.selected_card_ids[:]

    gen_req = GenerateInferenceRequest(
        case_id=case.case_id,
        selected_card_ids=selected_meta_ids,
        mode="hypothesis",
        user_prompt=req.user_prompt,
    )
    cards, edges, workspace, msg = await generate_inference_with_llm(case, gen_req)

    case.inference_cards.extend(cards)
    case.edges.extend(edges)
    case.workspace_state = workspace
    case.updated_at = _now()

    text = req.user_prompt.lower()
    recommended = RecommendedView.hypothesis
    if "time" in text or "timeline" in text or "时间" in text:
        recommended = RecommendedView.timeline
    elif "conflict" in text or "冲突" in text:
        recommended = RecommendedView.conflict
    elif "chain" in text or "链" in text:
        recommended = RecommendedView.evidence_chain

    missing_information: list[str] = []
    if not req.selected_info_units and not req.selected_relations:
        missing_information.append("No selected info units or relations were provided.")

    response = ReasoningResponse(
        success=True,
        recommended_view=recommended,
        reasoning_structure={
            "selected_info_units": req.selected_info_units,
            "selected_relations": req.selected_relations,
            "generated_cards": [c.id for c in cards],
        },
        view_payload={
            "focus_card_id": workspace.focused_card_id,
            "selected_card_ids": workspace.selected_card_ids,
        },
        missing_information=missing_information,
        reasoning_rationale="Inference cards are generated from current selection and user prompt, with evidence-linked edges.",
        new_inference_cards=cards,
        new_edges=edges,
        message=msg,
    )

    _append_agent_run(case, RouterTask.reasoning, "generate_reasoning", req.model_dump(), response.model_dump(mode="json"))
    return response


async def run_qa_agent(case: CaseData, req: QARequest) -> QAResponse:
    q = req.question.strip().lower()
    matched: list[str] = []
    for info in case.info_units:
        hay = f"{info.title} {info.summary} {info.detail}".lower()
        if q and q in hay:
            matched.append(info.id)

    if not matched and case.info_units:
        matched = [case.info_units[0].id]

    answer = "No matched information found."
    if matched:
        first = next((item for item in case.info_units if item.id == matched[0]), None)
        if first:
            answer = f"Matched {len(matched)} item(s). Primary match: {first.title}. {first.summary}"

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
    elif any(k in text for k in ["rearrange", "重排", "布局"]):
        intent = IntentType.canvas_edit
        mode = ResponseMode.canvas
        actions.append(UIAction(action=UIActionType.rearrange, targets=req.current_selection, params={"layout": "auto"}))
        message = "Requested canvas rearrangement."
    elif any(k in text for k in ["create", "新增", "创建"]):
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
















