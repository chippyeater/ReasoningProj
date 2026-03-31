"""Case service helpers for file ingestion and graph updates."""

from __future__ import annotations

import asyncio
import json
import os
import re
from io import BytesIO
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Sequence, cast
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
    LawRetrieveRequest,
    LawRetrieveResponse,
    ClaimSide,
    ClaimSubtype,
    EventSubtype,
    InfoUnit,
    InfoUnitSubtype,
    InfoUnitType,
    StateSubtype,
    SubjectSubtype,
    InferenceCard,
    InferenceDecision,
    InferenceDetail,
    InferenceType,
    IntentType,
    InteractionRequest,
    InteractionResponse,
    InteractionTrackRequest,
    LawCard,
    LawDetail,
    MetaCard,
    NormLevel,
    NormSourceType,
    MetaDetail,
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
LAW_CORPUS_PATH = Path(__file__).resolve().parent.parent / "data" / "laws.json"
PROMPT_FILE_BY_TASK = {
    "router": "router_system_prompt.md",
    "extraction": "extraction_system_prompt.md",
    "relation": "relation_system_prompt.md",
    "reasoning_selector": "reasoning_selector_prompt.md",
    "reasoning_composer": "reasoning_composer_prompt.md",
    "hypothesis_composer": "hypothesis_composer_prompt.md",
    "timeline_composer": "timeline_composer_prompt.md",
    "conflict_layer_composer": "conflict_layer_composer_prompt.md",
    "law_retrieve": "law_retrieve_system_prompt.md",
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
        info_type=InfoUnitType.claim,
        info_subtype=InfoUnitSubtype.fact_assertion,
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
        law_cards=[],
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
    # NOTE: 暂不重复执行 extraction agent，避免覆盖已有结果
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

    if not updated:
        for card in case.law_cards:
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
                card.detail = LawDetail.model_validate(data)
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


def _fallback_issue_conflict_generation(
    case: CaseData,
    request: GenerateInferenceRequest,
) -> tuple[list[InferenceCard], list[GraphEdge], WorkspaceState, str] | None:
    if request.mode != "conflict_check" or request.selected_card_ids:
        return None

    issue_targets = list(case.issue_set or _derive_issue_set_from_units(case.info_units, case.core_issue))
    if not issue_targets:
        return None

    existing_conflicts = {
        (card.title or "").strip().casefold(): card.id
        for card in case.inference_cards
        if card.inference_type == InferenceType.conflict
    }

    new_cards: list[InferenceCard] = []
    new_edges: list[GraphEdge] = []
    selected_conflict_ids: list[str] = []

    for issue in issue_targets:
        issue_key = issue.casefold()
        if issue_key in existing_conflicts:
            selected_conflict_ids.append(existing_conflicts[issue_key])
            continue

        related_units = [
            unit for unit in case.info_units
            if unit.type == InfoUnitType.claim and (unit.stance_target or "").casefold() == issue_key
        ]
        if not related_units:
            continue

        related_meta_ids = [f"meta-{unit.id}" for unit in related_units]
        landlord_ids = [f"meta-{unit.id}" for unit in related_units if unit.side == ClaimSide.landlord]
        tenant_ids = [f"meta-{unit.id}" for unit in related_units if unit.side == ClaimSide.tenant]
        neutral_ids = [f"meta-{unit.id}" for unit in related_units if unit.side in {ClaimSide.neutral, ClaimSide.unknown, None}]

        if not landlord_ids and related_meta_ids:
            landlord_ids = [related_meta_ids[0]]
        if not tenant_ids:
            tenant_ids = [meta_id for meta_id in related_meta_ids if meta_id not in landlord_ids]
        if not tenant_ids and neutral_ids:
            tenant_ids = neutral_ids[1:] if len(neutral_ids) > 1 else []

        card = _create_inference_card(claim=issue, selected_ids=related_meta_ids, mode="conflict_check", title=issue)
        card.summary = f"Conflict comparison view for issue: {issue}"
        card.detail.supporting_card_ids = landlord_ids or related_meta_ids[:1]
        card.detail.opposing_card_ids = tenant_ids
        card.detail.reasoning_steps = [
            f"Generated from issue_set target: {issue}",
            f"Grouped {len(related_units)} claim info units by stance_target.",
        ]
        new_cards.append(card)
        selected_conflict_ids.append(card.id)

        for meta_id in card.detail.supporting_card_ids:
            new_edges.append(
                GraphEdge(
                    id=f"edge-{uuid4().hex[:10]}",
                    source=meta_id,
                    target=card.id,
                    edge_type=EdgeType.supports,
                    label="issue_support",
                    created_at=_now(),
                )
            )
        for meta_id in card.detail.opposing_card_ids:
            new_edges.append(
                GraphEdge(
                    id=f"edge-{uuid4().hex[:10]}",
                    source=meta_id,
                    target=card.id,
                    edge_type=EdgeType.opposes,
                    label="issue_oppose",
                    created_at=_now(),
                )
            )
        for meta_id in related_meta_ids:
            if meta_id in card.detail.supporting_card_ids or meta_id in card.detail.opposing_card_ids:
                continue
            new_edges.append(
                GraphEdge(
                    id=f"edge-{uuid4().hex[:10]}",
                    source=meta_id,
                    target=card.id,
                    edge_type=EdgeType.relates_to,
                    label="issue_related",
                    created_at=_now(),
                )
            )

    if not selected_conflict_ids:
        return None

    workspace = case.workspace_state.model_copy(deep=True)
    workspace.focused_card_id = selected_conflict_ids[0]
    workspace.selected_card_ids = selected_conflict_ids
    message = "issue-set-conflict-fallback-generated" if new_cards else "issue-set-conflict-already-exists"
    return new_cards, new_edges, workspace, message


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


def _normalize_optional_text(value: object) -> str | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw or raw.lower() in {"none", "null", "n/a", "unknown"}:
        return None
    return raw


def _normalize_subject_legal_type(value: object, info_type: InfoUnitType):
    from app.schemas import SubjectLegalType

    if info_type != InfoUnitType.subject:
        return None
    normalized = _normalize_optional_text(value)
    if normalized is None:
        return None
    try:
        return SubjectLegalType(normalized.lower())
    except Exception:
        return None


def _normalize_claim_side(value: object):
    if value is None:
        return None
    raw = str(value).strip().lower()
    if not raw:
        return None
    aliases = {
        "landlord": ClaimSide.landlord,
        "tenant": ClaimSide.tenant,
        "neutral": ClaimSide.neutral,
        "unknown": ClaimSide.unknown,
        "lessor": ClaimSide.landlord,
        "lessee": ClaimSide.tenant,
        "owner": ClaimSide.landlord,
        "renter": ClaimSide.tenant,
    }
    return aliases.get(raw, ClaimSide.unknown)


def _normalize_issue_set(values: object) -> list[str]:
    if not isinstance(values, list):
        return []
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        raw = _normalize_optional_text(value)
        if raw is None:
            continue
        key = raw.casefold()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(raw)
    return normalized


def _derive_issue_set_from_units(units: Sequence[InfoUnit], core_issue: str | None = None) -> list[str]:
    issues: list[str] = []
    seen: set[str] = set()
    if core_issue:
        issues.append(core_issue)
        seen.add(core_issue.casefold())
    for unit in units:
        if unit.type != InfoUnitType.claim or not unit.stance_target:
            continue
        key = unit.stance_target.casefold()
        if key in seen:
            continue
        seen.add(key)
        issues.append(unit.stance_target)
    return issues


def _compact_card_for_reasoning(card: MetaCard | InferenceCard | LawCard, selected_ids: set[str]) -> dict:
    if isinstance(card, MetaCard):
        semantic_type = f"meta:{card.info_type.value}:{card.info_subtype.value}"
    elif isinstance(card, InferenceCard):
        semantic_type = f"inference:{card.inference_type.value}:{card.decision.value}"
    else:
        semantic_type = f"law:{card.source_type.value}:{card.norm_level.value}"

    return {
        "id": card.id,
        "type": semantic_type,
        "title": card.title,
        "summary": card.summary,
        "selected": card.id in selected_ids or bool(card.ui_state.selected),
        "detail": card.detail.model_dump(mode="json"),
    }


def _compact_edge_for_reasoning(edge: GraphEdge) -> dict:
    return {
        "id": edge.id,
        "source": edge.source,
        "target": edge.target,
        "edge_type": edge.edge_type.value,
        "label": edge.label,
    }


def _load_law_corpus() -> list[dict]:
    if not LAW_CORPUS_PATH.exists():
        return []
    try:
        data = json.loads(LAW_CORPUS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []
    return [item for item in data if isinstance(item, dict)]


def _extract_retrieval_terms(texts: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    terms: list[str] = []
    stopwords = {
        "的", "了", "和", "与", "及", "并", "或", "中", "对", "将", "把",
        "在", "是", "就", "也", "而", "被", "由", "于", "一个", "一种", "这个",
    }
    for raw_text in texts:
        text = (raw_text or "").strip()
        if not text:
            continue
        for token in re.findall(r"[一-鿿]{2,12}|[A-Za-z]{3,}", text):
            normalized = token.strip().lower()
            if not normalized or normalized in stopwords or len(normalized) < 2:
                continue
            if normalized in seen:
                continue
            seen.add(normalized)
            terms.append(normalized)
    return terms[:24]


def _score_law_candidate(item: dict, terms: Sequence[str]) -> int:
    title = str(item.get("title", "")).lower()
    article_no = str(item.get("article_no", "")).lower()
    text = str(item.get("text", "")).lower()
    keywords = [str(keyword).lower() for keyword in item.get("keywords", [])]
    score = 0
    for term in terms:
        if term in keywords:
            score += 8
        if term and term in title:
            score += 6
        if term and term in article_no:
            score += 4
        if term and term in text:
            score += 3
        for keyword in keywords:
            if term in keyword or keyword in term:
                score += 2
    return score


def _card_title(case: CaseData, card_id: str) -> str | None:
    for collection in (case.meta_cards, case.inference_cards, case.law_cards):
        match = next((card for card in collection if card.id == card_id), None)
        if match is not None:
            return match.title
    return None


def _build_law_retrieve_candidates(case: CaseData, conflict: InferenceCard, req: LawRetrieveRequest) -> tuple[list[dict], list[str]]:
    related_ids = list(
        dict.fromkeys(
            [conflict.id]
            + req.selected_card_ids
            + conflict.detail.supporting_card_ids
            + conflict.detail.opposing_card_ids
            + conflict.detail.missing_card_ids
        )
    )
    texts = [
        conflict.title,
        conflict.summary or "",
        conflict.detail.claim,
        *(conflict.detail.reasoning_steps or []),
        *(filter(None, (_card_title(case, card_id) for card_id in related_ids))),
        req.query_hint or "",
    ]
    terms = _extract_retrieval_terms(texts)
    ranked = sorted(
        (
            {
                **item,
                "_score": _score_law_candidate(item, terms),
            }
            for item in _load_law_corpus()
        ),
        key=lambda item: item["_score"],
        reverse=True,
    )
    filtered = [item for item in ranked if item["_score"] > 0]
    if not filtered:
        filtered = ranked
    return filtered[: max(req.max_results * 2, 6)], terms


def _build_law_retrieve_prompt(
    case: CaseData,
    conflict: InferenceCard,
    req: LawRetrieveRequest,
    candidates: Sequence[dict],
    terms: Sequence[str],
) -> str:
    related_cards = []
    related_ids = list(
        dict.fromkeys(req.selected_card_ids + conflict.detail.supporting_card_ids + conflict.detail.opposing_card_ids)
    )
    selected_ids = set(related_ids)
    for card in case.meta_cards + case.inference_cards + case.law_cards:
        if card.id in selected_ids:
            related_cards.append(_compact_card_for_reasoning(card, selected_ids))

    candidate_payload = [
        {
            "id": item.get("id"),
            "title": item.get("title"),
            "article_no": item.get("article_no"),
            "doc_type": item.get("doc_type"),
            "effective_status": item.get("effective_status"),
            "keywords": item.get("keywords", []),
            "text": item.get("text"),
            "source_url": item.get("source_url"),
            "keyword_score": item.get("_score", 0),
        }
        for item in candidates
    ]

    return (
        f"Case title:\n{case.case_title}\n\n"
        f"Intent: {req.intent.value}\n"
        f"Conflict card id: {conflict.id}\n"
        f"Conflict title: {conflict.title}\n"
        f"Conflict summary: {conflict.summary or ''}\n"
        f"Conflict claim: {conflict.detail.claim}\n"
        f"Query hint: {req.query_hint or '[empty]'}\n"
        f"Keyword terms: {json.dumps(list(terms), ensure_ascii=False)}\n\n"
        f"Related cards:\n{json.dumps(related_cards, ensure_ascii=False, indent=2)}\n\n"
        f"Candidate laws:\n{json.dumps(candidate_payload, ensure_ascii=False, indent=2)}\n\n"
        "Return strict JSON with keys: selected_laws.\n"
        "selected_laws: list of {law_id, reason, applies_to_card_ids}.\n"
        "law_id must come from candidate laws only.\n"
        "applies_to_card_ids must refer to existing related card ids only.\n"
        "Pick the most relevant laws only. No markdown, no extra text."
    )


def _law_doc_type_to_source_type(doc_type: str) -> NormSourceType:
    raw = (doc_type or "").strip().lower()
    if raw == "law":
        return NormSourceType.statute
    if raw == "judicial_interpretation":
        return NormSourceType.judicial_interpretation
    return NormSourceType.other


def _law_doc_type_to_norm_level(doc_type: str) -> NormLevel:
    raw = (doc_type or "").strip().lower()
    if raw == "law":
        return NormLevel.law
    if raw == "judicial_interpretation":
        return NormLevel.judicial_interpretation
    return NormLevel.other


def _source_info_unit_ids_from_cards(card_ids: Sequence[str]) -> list[str]:
    info_ids: list[str] = []
    for card_id in card_ids:
        if card_id.startswith("meta-info-"):
            info_ids.append(card_id.removeprefix("meta-"))
    return list(dict.fromkeys(info_ids))


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
        "inference_type must be one of: hypothesis, conclusion, conflict, timeline, missing_evidence, risk, other.\n"
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
    if request.mode == "auto":
        reasoning_req = ReasoningRequest(
            case_id=case.case_id,
            selected_info_units=[card_id.removeprefix("meta-") for card_id in request.selected_card_ids if card_id.startswith("meta-")],
            selected_relations=[],
            user_prompt=request.user_prompt or "",
            current_canvas_state={},
        )
        response, cards, edges, workspace = await _generate_reasoning_view_artifacts(case, reasoning_req)
        return cards, edges, workspace, response.message or "reasoning-view-generated"

    issue_fallback = _fallback_issue_conflict_generation(case, request)
    if issue_fallback is not None:
        return issue_fallback

    fallback_cards, fallback_edges, fallback_workspace = generate_inference(case, request)

    system_prompt = _get_system_prompt("reasoning_composer")
    user_prompt = _build_inference_prompt(case, request)
    selected_ids = set(request.selected_card_ids)
    sent_sections = [
        "case_title",
        "mode",
        "user_prompt",
        "focused_card_id",
        "selected_card_ids",
        "all_meta_cards",
        "all_inference_cards",
        "all_law_cards",
        "all_edges",
    ]
    request_snapshot = {
        "mode": request.mode,
        "user_prompt": request.user_prompt or "",
        "focused_card_id": case.workspace_state.focused_card_id,
        "selected_card_ids": request.selected_card_ids,
        "all_meta_cards": [_compact_card_for_reasoning(c, selected_ids) for c in case.meta_cards],
        "all_inference_cards": [_compact_card_for_reasoning(c, selected_ids) for c in case.inference_cards],
        "all_law_cards": [_compact_card_for_reasoning(c, selected_ids) for c in case.law_cards],
        "all_edges": [_compact_edge_for_reasoning(e) for e in case.edges],
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
            card = _create_inference_card(claim=claim, selected_ids=[str(i) for i in supporting], mode=request.mode, title=title)
            summary = str(item.get("summary", "")).strip()
            if summary:
                card.summary = summary
            opposing = item.get("opposing_card_ids", [])
            missing = item.get("missing_card_ids", [])
            legal_basis = item.get("legal_basis_ids", [])
            reasoning_steps = item.get("reasoning_steps", [])
            confidence = item.get("confidence")
            card.detail.opposing_card_ids = [str(i) for i in opposing] if isinstance(opposing, list) else []
            card.detail.missing_card_ids = [str(i) for i in missing] if isinstance(missing, list) else []
            card.detail.legal_basis_ids = [str(i) for i in legal_basis] if isinstance(legal_basis, list) else []
            card.detail.reasoning_steps = [str(step).strip() for step in reasoning_steps if str(step).strip()] if isinstance(reasoning_steps, list) else card.detail.reasoning_steps
            if isinstance(confidence, (int, float)):
                card.detail.confidence = float(confidence)
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






async def run_law_retrieve(case: CaseData, req: LawRetrieveRequest) -> LawRetrieveResponse:
    conflict = next((card for card in case.inference_cards if card.id == req.conflict_card_id), None)
    if conflict is None:
        return LawRetrieveResponse(
            success=False,
            conflict_card_id=req.conflict_card_id,
            message="conflict-card-not-found",
        )

    if conflict.inference_type != InferenceType.conflict:
        return LawRetrieveResponse(
            success=False,
            conflict_card_id=req.conflict_card_id,
            message="target-card-is-not-conflict",
        )

    if conflict.decision != InferenceDecision.accepted:
        return LawRetrieveResponse(
            success=False,
            conflict_card_id=req.conflict_card_id,
            message="conflict-card-must-be-accepted",
        )

    workspace = case.workspace_state.model_copy(deep=True)
    workspace.focused_card_id = conflict.id
    workspace.selected_card_ids = [conflict.id]

    candidates, terms = _build_law_retrieve_candidates(case, conflict, req)
    if not candidates:
        _append_agent_run(
            case,
            RouterTask.law_retrieve,
            "retrieve_law",
            req.model_dump(mode="json"),
            {"keyword_terms": terms, "candidate_count": 0, "new_law_card_count": 0, "new_edge_count": 0},
            status="fallback",
            error="law-corpus-empty",
        )
        return LawRetrieveResponse(
            success=False,
            conflict_card_id=req.conflict_card_id,
            new_law_cards=[],
            new_edges=[],
            updated_workspace_state=workspace,
            message="law-corpus-empty",
        )

    selected_matches: list[dict] = []
    rerank_message = "law-retrieve-keyword-only"
    raw_reply = ""
    parsed: dict[str, Any] | dict = {}
    request_meta: dict[str, Any] = {}
    response_meta: dict[str, Any] = {}
    request_snapshot = {
        "intent": req.intent.value,
        "conflict_card_id": req.conflict_card_id,
        "selected_card_ids": req.selected_card_ids,
        "query_hint": req.query_hint or "",
        "max_results": req.max_results,
        "keyword_terms": terms,
        "conflict_card": _compact_card_for_reasoning(conflict, set(req.selected_card_ids + [conflict.id])),
        "candidate_laws": [
            {
                "id": item.get("id"),
                "title": item.get("title"),
                "article_no": item.get("article_no"),
                "doc_type": item.get("doc_type"),
                "effective_status": item.get("effective_status"),
                "keywords": item.get("keywords", []),
                "keyword_score": item.get("_score", 0),
            }
            for item in candidates
        ],
    }

    try:
        system_prompt = _get_system_prompt("law_retrieve")
        user_prompt = _build_law_retrieve_prompt(case, conflict, req, candidates, terms)
        llm_result = await asyncio.to_thread(_call_llm_sync, system_prompt, user_prompt)
        parsed = llm_result.get("parsed_json", {})
        raw_reply = str(llm_result.get("raw_reply", ""))
        request_meta = llm_result.get("request_meta", {})
        response_meta = llm_result.get("response_meta", {})
        raw_selected = parsed.get("selected_laws", []) if isinstance(parsed, dict) else []
        if isinstance(raw_selected, list):
            candidate_ids = {str(item.get("id")) for item in candidates}
            valid_related_ids = set(req.selected_card_ids + conflict.detail.supporting_card_ids + conflict.detail.opposing_card_ids + [conflict.id])
            for item in raw_selected[: req.max_results]:
                if not isinstance(item, dict):
                    continue
                law_id = str(item.get("law_id", "")).strip()
                if law_id not in candidate_ids:
                    continue
                applies_to_card_ids = item.get("applies_to_card_ids", [])
                if not isinstance(applies_to_card_ids, list):
                    applies_to_card_ids = []
                selected_matches.append(
                    {
                        "law_id": law_id,
                        "reason": str(item.get("reason", "")).strip(),
                        "applies_to_card_ids": [
                            card_id
                            for card_id in (str(v) for v in applies_to_card_ids)
                            if card_id in valid_related_ids
                        ],
                    }
                )
        if selected_matches:
            rerank_message = "law-retrieve-llm-reranked"
    except Exception as exc:
        raw_reply = str(exc)

    if not selected_matches:
        selected_matches = [
            {
                "law_id": str(item.get("id")),
                "reason": "Keyword retrieval fallback.",
                "applies_to_card_ids": req.selected_card_ids[:1] or conflict.detail.supporting_card_ids[:1] or [conflict.id],
            }
            for item in candidates[: req.max_results]
        ]

    existing_law_cards = {card.id: card for card in case.law_cards}
    existing_edge_keys = {(edge.source, edge.target, edge.edge_type.value) for edge in case.edges}
    candidate_map = {str(item.get("id")): item for item in candidates}
    conflict_point = conflict.position
    new_law_cards: list[LawCard] = []
    new_edges: list[GraphEdge] = []
    selected_law_ids: list[str] = [conflict.id]

    for index, match in enumerate(selected_matches):
        law_record = candidate_map.get(match["law_id"])
        if law_record is None:
            continue
        law_card_id = f"lawref-{law_record['id']}"
        law_card = existing_law_cards.get(law_card_id)
        if law_card is None:
            now = _now()
            law_card = LawCard(
                id=law_card_id,
                title=f"{law_record.get('title', '')} {law_record.get('article_no', '')}".strip(),
                summary=match["reason"] or str(law_record.get("text", "")).strip()[:120],
                display_level=DisplayLevel.level_2,
                position=CardPosition(
                    x=(conflict_point.x if conflict_point else 920) + 460,
                    y=(conflict_point.y if conflict_point else 220) + index * 176,
                ),
                source_file_ids=[],
                created_at=now,
                updated_at=now,
                source_type=_law_doc_type_to_source_type(str(law_record.get("doc_type", ""))),
                norm_level=_law_doc_type_to_norm_level(str(law_record.get("doc_type", ""))),
                detail=LawDetail(
                    source_type=_law_doc_type_to_source_type(str(law_record.get("doc_type", ""))),
                    norm_level=_law_doc_type_to_norm_level(str(law_record.get("doc_type", ""))),
                    article_no=_normalize_optional_text(law_record.get("article_no")),
                    title_full=str(law_record.get("title", "")).strip(),
                    effective_status=_normalize_optional_text(law_record.get("effective_status")),
                    source_url=_normalize_optional_text(law_record.get("source_url")),
                    keywords=[str(keyword).strip() for keyword in law_record.get("keywords", []) if str(keyword).strip()],
                    text=_normalize_optional_text(law_record.get("text")),
                    source_info_unit_ids=_source_info_unit_ids_from_cards(
                        match["applies_to_card_ids"] or req.selected_card_ids or conflict.detail.supporting_card_ids
                    ),
                ),
            )
            new_law_cards.append(law_card)

        selected_law_ids.append(law_card.id)

        desired_edges = [
            (law_card.id, conflict.id, EdgeType.retrieved_for, "检索"),
            (law_card.id, conflict.id, EdgeType.grounded_in, "依据"),
        ]
        for applies_to_id in match["applies_to_card_ids"]:
            desired_edges.append((law_card.id, applies_to_id, EdgeType.applies_to, "适用"))

        for source, target, edge_type, label in desired_edges:
            key = (source, target, edge_type.value)
            if key in existing_edge_keys:
                continue
            existing_edge_keys.add(key)
            new_edges.append(
                GraphEdge(
                    id=f"edge-{uuid4().hex[:10]}",
                    source=source,
                    target=target,
                    edge_type=edge_type,
                    label=label,
                    created_at=_now(),
                )
            )

    law_basis_ids = list(dict.fromkeys(selected_law_ids[1:]))
    if law_basis_ids:
        conflict.detail.legal_basis_ids = list(dict.fromkeys([*conflict.detail.legal_basis_ids, *law_basis_ids]))
        updated_at = _now()
        conflict.updated_at = updated_at
        case.updated_at = updated_at

    workspace.selected_card_ids = list(dict.fromkeys(selected_law_ids))

    append_case_log(
        case.case_id,
        "llm",
        {
            "stage": "law_retrieve",
            "status": "success" if new_law_cards or selected_matches else "fallback",
            "message": rerank_message,
            "fallback_reason": "" if new_law_cards or selected_matches else "law-retrieve-empty",
            "error_code": response_meta.get("http_status"),
            "request_meta": request_meta,
            "request_snapshot": request_snapshot,
            "raw_reply": raw_reply,
            "parsed_snapshot": parsed,
            "response_meta": response_meta,
            "conflict_card_id": req.conflict_card_id,
            "query_terms": terms,
            "candidate_count": len(candidates),
            "selected_law_ids": [match["law_id"] for match in selected_matches],
            "new_law_card_count": len(new_law_cards),
            "new_edge_count": len(new_edges),
        },
    )
    _append_agent_run(
        case,
        RouterTask.law_retrieve,
        "retrieve_law",
        req.model_dump(mode="json"),
        {
            "keyword_terms": terms,
            "candidate_count": len(candidates),
            "selected_law_ids": [match["law_id"] for match in selected_matches],
            "new_law_card_count": len(new_law_cards),
            "new_edge_count": len(new_edges),
        },
    )

    return LawRetrieveResponse(
        success=True,
        conflict_card_id=req.conflict_card_id,
        new_law_cards=new_law_cards,
        new_edges=new_edges,
        updated_workspace_state=workspace,
        message=rerank_message,
    )


async def route_task(case: CaseData, req: RouteRequest) -> RouteResponse:
    text = req.user_input.strip().lower()
    task = RouterTask.qa
    subtask = "answer"
    requires_tool = False

    if any(k in text for k in ["upload", "parse", "extract", "上传", "提取"]):
        task = RouterTask.extraction
        subtask = "extract_info_units"
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



def _normalize_confidence(value: str | None) -> ConfidenceLevel:
    raw = (value or "").strip().lower()
    if raw == "high":
        return ConfidenceLevel.high
    if raw == "low":
        return ConfidenceLevel.low
    return ConfidenceLevel.medium


def _normalize_info_type(value: str | None) -> InfoUnitType:
    raw = (value or "").strip().lower()
    aliases = {
        "person": InfoUnitType.subject,
        "organization": InfoUnitType.subject,
        "org": InfoUnitType.subject,
        "object": InfoUnitType.state,
        "time": InfoUnitType.state,
        "location": InfoUnitType.state,
        "rule": InfoUnitType.claim,
        "law": InfoUnitType.claim,
    }
    if raw in aliases:
        return aliases[raw]
    try:
        return InfoUnitType(raw)
    except Exception:
        return InfoUnitType.claim


def _default_subtype_for_type(info_type: InfoUnitType) -> InfoUnitSubtype:
    if info_type == InfoUnitType.subject:
        return InfoUnitSubtype.person
    if info_type == InfoUnitType.event:
        return InfoUnitSubtype.factual_act
    if info_type == InfoUnitType.state:
        return InfoUnitSubtype.physical_state
    return InfoUnitSubtype.fact_assertion


def _normalize_info_subtype(info_type: InfoUnitType, value: str | None) -> InfoUnitSubtype:
    raw = (value or "").strip().lower()

    alias_to_subtype: dict[str, InfoUnitSubtype] = {
        "person": InfoUnitSubtype.person,
        "organization": InfoUnitSubtype.organization,
        "org": InfoUnitSubtype.organization,
        "legal_act": InfoUnitSubtype.legal_act,
        "factual_act": InfoUnitSubtype.factual_act,
        "transaction": InfoUnitSubtype.transaction,
        "communication": InfoUnitSubtype.communication,
        "violation": InfoUnitSubtype.violation,
        "temporal": InfoUnitSubtype.temporal,
        "physical_state": InfoUnitSubtype.physical_state,
        "usage_state": InfoUnitSubtype.usage_state,
        "fact_assertion": InfoUnitSubtype.fact_assertion,
        "legal_assertion": InfoUnitSubtype.legal_assertion,
        "defense": InfoUnitSubtype.defense,
        # compatibility aliases from old extraction outputs
        "object": InfoUnitSubtype.physical_state,
        "time": InfoUnitSubtype.temporal,
        "location": InfoUnitSubtype.physical_state,
        "claim": InfoUnitSubtype.fact_assertion,
        "event": InfoUnitSubtype.factual_act,
        "rule": InfoUnitSubtype.legal_assertion,
        "law": InfoUnitSubtype.legal_assertion,
        "formal_rule": InfoUnitSubtype.legal_assertion,
        "informal_rule": InfoUnitSubtype.fact_assertion,
        "default_rule": InfoUnitSubtype.fact_assertion,
    }

    candidate = alias_to_subtype.get(raw)

    allowed: dict[InfoUnitType, set[InfoUnitSubtype]] = {
        InfoUnitType.subject: {InfoUnitSubtype.person, InfoUnitSubtype.organization},
        InfoUnitType.event: {
            InfoUnitSubtype.legal_act,
            InfoUnitSubtype.factual_act,
            InfoUnitSubtype.transaction,
            InfoUnitSubtype.communication,
            InfoUnitSubtype.violation,
        },
        InfoUnitType.state: {InfoUnitSubtype.temporal, InfoUnitSubtype.physical_state, InfoUnitSubtype.usage_state},
        InfoUnitType.claim: {InfoUnitSubtype.fact_assertion, InfoUnitSubtype.legal_assertion, InfoUnitSubtype.defense},
    }

    if candidate and candidate in allowed.get(info_type, set()):
        return candidate
    return _default_subtype_for_type(info_type)


def _normalize_certainty(value: str | None) -> CertaintyLevel:
    raw = (value or "").strip().lower()
    if raw == "inferred":
        return CertaintyLevel.inferred
    return CertaintyLevel.explicit


def _relation_type_to_edge_type(relation_type: str) -> EdgeType:
    raw = (relation_type or "").strip().lower()
    if raw in {"support", "supports"}:
        return EdgeType.supports
    if raw in {"oppose", "opposes"}:
        return EdgeType.opposes
    if raw in {"contradict", "contradicts", "conflict", "conflicts_with"}:
        return EdgeType.conflicts_with
    if raw in {"derive", "derives"}:
        return EdgeType.derives
    if raw in {"belong_to", "belongs_to"}:
        return EdgeType.belongs_to
    if raw in {"refer_to", "mentions", "mention"}:
        return EdgeType.mentions
    if raw in {"cite", "cites"}:
        return EdgeType.cites
    if raw in {"grounded_in"}:
        return EdgeType.grounded_in
    if raw in {"applies_to"}:
        return EdgeType.applies_to
    if raw in {"retrieved_for"}:
        return EdgeType.retrieved_for
    if raw in {"missing_for"}:
        return EdgeType.missing_for
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
                type=InfoUnitType.claim if text else InfoUnitType.state,
                title=file_item.filename,
                subtype=InfoUnitSubtype.fact_assertion if text else InfoUnitSubtype.physical_state,
                subject_legal_type=None,
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
                subtype=InfoUnitSubtype.fact_assertion,
                subject_legal_type=None,
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
                info_type=info.type,
                info_subtype=info.subtype,
                detail=MetaDetail(
                    name=info.title,
                    description=info.detail,
                    tags=[f"confidence:{info.confidence.value}", "extracted"],
                    attributes={
                        "info_type": info.type.value,
                        "info_subtype": info.subtype.value,
                        "subject_legal_type": info.subject_legal_type.value if info.subject_legal_type else None,
                        "actor": info.actor,
                        "target": info.target,
                        "speaker": info.speaker,
                        "side": info.side.value if info.side else None,
                        "stance_target": info.stance_target,
                        "extraction_reason": info.extraction_reason,
                        "evidence_quote": info.evidence_quote,
                    },
                ),
            )
        )
    case.meta_cards = synced_cards


def _sync_law_cards_from_info_units(case: CaseData, units: list[InfoUnit]) -> None:
    # Law cards should only be created by a dedicated law retrieval stage.
    case.law_cards = []


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
        + "\n\nReturn strict JSON with core_issue, issue_set, and info_units."
    )

    units: list[InfoUnit] = []
    try:
        llm_result = await asyncio.to_thread(_call_llm_sync, system_prompt, user_prompt)
        parsed = llm_result.get("parsed_json", {})
        raw_reply = str(llm_result.get("raw_reply", ""))
        request_meta = llm_result.get("request_meta", {})
        response_meta = llm_result.get("response_meta", {})
        raw_units = parsed.get("info_units", []) if isinstance(parsed, dict) else []
        parsed_core_issue = _normalize_optional_text(parsed.get("core_issue")) if isinstance(parsed, dict) else None
        parsed_issue_set = _normalize_issue_set(parsed.get("issue_set")) if isinstance(parsed, dict) else []

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
                        type=(unit_type := _normalize_info_type(str(item.get("type", "")))),
                        subtype=_normalize_info_subtype(unit_type, str(item.get("subtype", ""))),
                        subject_legal_type=_normalize_subject_legal_type(item.get("subject_legal_type", item.get("legal_type")), unit_type),
                        title=title,
                        summary=summary[:300],
                        detail=detail[:2000],
                        actor=_normalize_optional_text(item.get("actor")) if unit_type == InfoUnitType.event else None,
                        target=_normalize_optional_text(item.get("target")) if unit_type == InfoUnitType.event else None,
                        speaker=_normalize_optional_text(item.get("speaker")) if unit_type == InfoUnitType.claim else None,
                        side=_normalize_claim_side(item.get("side")) if unit_type == InfoUnitType.claim else None,
                        stance_target=_normalize_optional_text(item.get("stance_target")) if unit_type == InfoUnitType.claim else None,
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
        case.core_issue = parsed_core_issue or case.core_issue
        case.issue_set = parsed_issue_set or _derive_issue_set_from_units(units, case.core_issue)
        _sync_meta_cards_from_info_units(case, units)
        _sync_law_cards_from_info_units(case, units)
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
        case.issue_set = _derive_issue_set_from_units(units, case.core_issue)
        _sync_meta_cards_from_info_units(case, units)
        _sync_law_cards_from_info_units(case, units)
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
        case.issue_set = _derive_issue_set_from_units(units, case.core_issue)
        _sync_meta_cards_from_info_units(case, units)
        _sync_law_cards_from_info_units(case, units)
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
                "subtype": n.subtype.value,
                "subject_legal_type": n.subject_legal_type.value if n.subject_legal_type else None,
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


def _compact_info_unit_for_reasoning_view(info: InfoUnit, selected_ids: set[str]) -> dict[str, object]:
    return {
        "id": info.id,
        "type": info.type.value,
        "subtype": info.subtype.value,
        "title": info.title,
        "summary": info.summary,
        "detail": info.detail,
        "selected": info.id in selected_ids,
        "speaker": info.speaker,
        "side": info.side.value if info.side else None,
        "stance_target": info.stance_target,
        "actor": info.actor,
        "target": info.target,
        "confidence": info.confidence.value,
        "source_refs": info.source_refs,
    }



def _compact_relation_for_reasoning_view(relation: RelationRecord, selected_ids: set[str]) -> dict[str, object]:
    return {
        "id": relation.id,
        "source_id": relation.source_id,
        "target_id": relation.target_id,
        "relation_type": relation.relation_type,
        "confidence": relation.confidence.value,
        "certainty_level": relation.certainty_level.value,
        "selected": relation.id in selected_ids,
        "evidence_basis": relation.evidence_basis,
        "rationale": relation.rationale,
    }



def _selected_reasoning_scope(case: CaseData, req: ReasoningRequest) -> tuple[list[InfoUnit], list[RelationRecord], list[str]]:
    selected_info_ids = set(req.selected_info_units)
    selected_relation_ids = set(req.selected_relations)
    selected_units = [unit for unit in case.info_units if unit.id in selected_info_ids]
    selected_relations = [relation for relation in case.relations if relation.id in selected_relation_ids]

    related_info_ids: set[str] = set(selected_info_ids)
    for relation in selected_relations:
        related_info_ids.add(relation.source_id)
        related_info_ids.add(relation.target_id)

    selected_meta_ids = [f"meta-{info_id}" for info_id in related_info_ids if any(card.id == f"meta-{info_id}" for card in case.meta_cards)]
    return selected_units, selected_relations, selected_meta_ids



def _build_reasoning_selector_snapshot(case: CaseData, req: ReasoningRequest) -> dict[str, object]:
    selected_info_ids = set(req.selected_info_units)
    selected_relation_ids = set(req.selected_relations)
    return {
        "user_question": (req.user_prompt or "").strip(),
        "core_issue": case.core_issue,
        "issue_set": case.issue_set,
        "selected_info_units": req.selected_info_units,
        "selected_relations": req.selected_relations,
        "info_units": [_compact_info_unit_for_reasoning_view(unit, selected_info_ids) for unit in case.info_units],
        "relations": [_compact_relation_for_reasoning_view(relation, selected_relation_ids) for relation in case.relations],
    }



def _build_reasoning_composer_snapshot(case: CaseData, req: ReasoningRequest, view_type: RecommendedView) -> dict[str, object]:
    selected_info_ids = set(req.selected_info_units)
    selected_relation_ids = set(req.selected_relations)
    return {
        "user_question": (req.user_prompt or "").strip(),
        "view_type": view_type.value,
        "core_issue": case.core_issue,
        "issue_set": case.issue_set,
        "selected_info_units": req.selected_info_units,
        "selected_relations": req.selected_relations,
        "info_units": [_compact_info_unit_for_reasoning_view(unit, selected_info_ids) for unit in case.info_units],
        "relations": [_compact_relation_for_reasoning_view(relation, selected_relation_ids) for relation in case.relations],
    }


def _reasoning_composer_task(view_type: RecommendedView) -> str:
    if view_type == RecommendedView.conflict:
        return "conflict_layer_composer"
    if view_type == RecommendedView.timeline:
        return "timeline_composer"
    return "hypothesis_composer"


def _reasoning_composer_output_instruction(view_type: RecommendedView) -> str:
    if view_type == RecommendedView.conflict:
        return "Return strict JSON with keys: view_type, title, layout_plan, reasoning_steps, recommended_resolution."
    if view_type == RecommendedView.timeline:
        return "Return strict JSON with keys: timeline_title, time_granularity, timepoints, events."
    return "Return strict JSON with keys: hypotheses, links, groups."


_DATE_PATTERN = re.compile(r"(20\d{2}(?:-\d{1,2})?(?:-\d{1,2})?(?:\s+\d{1,2}:\d{2})?)")


def _extract_time_sort_value(texts: Sequence[str]) -> str:
    for text in texts:
        match = _DATE_PATTERN.search(text)
        if match:
            return match.group(1)
    return ""


def _time_precision_from_sort_value(value: str) -> str:
    if not value:
        return "unknown"
    if re.fullmatch(r"20\d{2}", value):
        return "year"
    if re.fullmatch(r"20\d{2}-\d{1,2}", value):
        return "month"
    if re.fullmatch(r"20\d{2}-\d{1,2}-\d{1,2}", value):
        return "day"
    if re.fullmatch(r"20\d{2}-\d{1,2}-\d{1,2}\s+\d{1,2}:\d{2}", value):
        return "hour"
    return "unknown"


def _hypothesis_stage_for_unit(unit: InfoUnit) -> str:
    if unit.type == InfoUnitType.claim and unit.subtype == InfoUnitSubtype.legal_assertion:
        return "legal_reasoning"
    if unit.type == InfoUnitType.claim and unit.subtype == InfoUnitSubtype.defense:
        return "legal_application"
    if unit.type == InfoUnitType.claim:
        return "final_claim"
    return "fact_recognition"


def _link_tone_for_relation(relation: str) -> str:
    if relation == "support":
        return "positive"
    if relation == "attack":
        return "negative"
    return "warning"



def _fallback_reasoning_selector(case: CaseData, req: ReasoningRequest) -> tuple[RecommendedView, str]:
    text = (req.user_prompt or "").lower()
    claims = [unit for unit in case.info_units if unit.type == InfoUnitType.claim]
    issue_targets = [unit.stance_target for unit in claims if unit.stance_target]
    has_conflict = bool(issue_targets) and any(unit.side in {ClaimSide.landlord, ClaimSide.tenant} for unit in claims)
    if any(keyword in text for keyword in ["冲突", "对比", "争议", "矛盾", "conflict"]):
        return RecommendedView.conflict, "User question explicitly asks for conflict comparison."
    if any(keyword in text for keyword in ["时间", "时间线", "timeline", "时序"]):
        return RecommendedView.timeline, "User question focuses on temporal order."
    if has_conflict:
        return RecommendedView.conflict, "Case contains opposing claims around a shared issue target."
    return RecommendedView.hypothesis, "Default to hypothesis view when no stronger conflict or timeline signal is present."



def _fallback_reasoning_composer(case: CaseData, req: ReasoningRequest, view_type: RecommendedView) -> dict[str, object]:
    selected_units, _selected_relations, _ = _selected_reasoning_scope(case, req)
    units = selected_units or case.info_units
    title_fallback = (req.user_prompt or case.core_issue or "Reasoning View").strip() or "Reasoning View"

    if view_type == RecommendedView.conflict:
        claims = [unit for unit in units if unit.type == InfoUnitType.claim]
        primary_issue = next((unit.stance_target for unit in claims if unit.stance_target), None) or case.core_issue or title_fallback
        landlord_claims = [unit for unit in claims if unit.side == ClaimSide.landlord and (not unit.stance_target or unit.stance_target == primary_issue)]
        tenant_claims = [unit for unit in claims if unit.side == ClaimSide.tenant and (not unit.stance_target or unit.stance_target == primary_issue)]
        neutral_claims = [unit for unit in claims if unit.side in {ClaimSide.neutral, ClaimSide.unknown, None} and (not unit.stance_target or unit.stance_target == primary_issue)]
        sections: list[dict[str, object]] = [
            {"section_id": "section_header", "section_type": "question_header", "title": primary_issue, "purpose": "Define the conflict question under review.", "placement": "top", "cards": []},
            {"section_id": "section_left", "section_type": "side_column", "title": "相关主张", "purpose": "Claims and evidence supporting the landlord side.", "placement": "left", "cards": [{"card_id": unit.id, "card_role": "claim", "emphasis": "high", "default_expanded": True} for unit in landlord_claims]},
            {"section_id": "section_right", "section_type": "side_column", "title": "相关主张", "purpose": "Claims and evidence supporting the tenant side.", "placement": "right", "cards": [{"card_id": unit.id, "card_role": "claim", "emphasis": "high", "default_expanded": True} for unit in tenant_claims]},
        ]
        if neutral_claims:
            sections.append({"section_id": "section_shared", "section_type": "shared_facts", "title": "相关主张", "purpose": "Facts or neutral statements shared by both sides.", "placement": "bottom", "cards": [{"card_id": unit.id, "card_role": "evidence", "emphasis": "medium", "default_expanded": False} for unit in neutral_claims]})
        return {
            "view_type": view_type.value,
            "title": primary_issue,
            "layout_plan": {"sections": sections},
            "reasoning_steps": [f"当前争议点是：{primary_issue}。"],
            "recommended_resolution": {
                "recommended_decision": "accepted" if landlord_claims and tenant_claims else "pending",
                "preferred_side": "neutral",
                "reason": "双方已围绕同一争议点形成可直接对比的对立主张。" if landlord_claims and tenant_claims else "当前对立结构尚不完整，需要继续补充证据。",
                "confidence": 0.74 if landlord_claims and tenant_claims else 0.46,
            },
        }

    if view_type == RecommendedView.timeline:
        timeline_units = [unit for unit in units if unit.type in {InfoUnitType.event, InfoUnitType.state}] or units[:5]
        timeline_units = timeline_units[:5]
        timepoints: list[dict[str, object]] = []
        events: list[dict[str, object]] = []
        for index, unit in enumerate(timeline_units):
            sort_value = _extract_time_sort_value([unit.title, unit.summary, unit.detail])
            event_id = f"ev_{unit.id}"
            timepoint_id = f"tp_{index + 1}"
            timepoints.append({
                "id": timepoint_id,
                "time_label": sort_value or unit.title[:12] or f"时间点{index + 1}",
                "time_sort_value": sort_value,
                "time_precision": _time_precision_from_sort_value(sort_value),
                "event_title": unit.title[:12] or f"事件{index + 1}",
                "event_brief": unit.summary[:32] or unit.detail[:32],
                "event_ids": [event_id],
                "certainty": unit.confidence.value,
                "is_key_event": index == 0,
                "point_index": index,
                "emphasis": "focused" if index == 0 else "normal",
            })
            events.append({
                "id": event_id,
                "timepoint_id": timepoint_id,
                "title": unit.title,
                "summary": unit.summary,
                "detail": unit.detail[:120],
                "full_description": unit.detail,
                "actors": [item for item in [unit.actor, unit.speaker] if item],
                "location": "",
                "event_type": "communication" if unit.subtype == InfoUnitSubtype.communication else "other",
                "preview_type": "none",
                "related_info_unit_ids": [unit.id],
                "source_excerpt": unit.evidence_quote[:160],
                "askable_questions": [f"{unit.title}发生在什么时候？", f"{unit.title}说明了什么？"],
                "importance": unit.confidence.value,
                "time_conflict_status": "none",
                "primary_detail_info_unit_id": unit.id,
                "preview_url": None,
            })
        detail_card = None
        if events:
            first_event = events[0]
            detail_card = {"event_id": first_event["id"], "title": first_event["title"], "detail": first_event["detail"], "card_label": "元信息", "preview_type": first_event["preview_type"], "preview_url": first_event["preview_url"], "related_info_unit_ids": first_event["related_info_unit_ids"], "source_excerpt": first_event["source_excerpt"]}
        return {
            "view_type": view_type.value,
            "timeline_title": title_fallback,
            "time_granularity": "mixed",
            "layout_mode": "fixed_five_point",
            "visible_point_count": 5,
            "focused_timepoint_id": timepoints[0]["id"] if timepoints else None,
            "expanded_event_id": events[0]["id"] if events else None,
            "zoom_behavior": "expand_detail_on_zoom",
            "timepoints": timepoints,
            "events": events,
            "detail_card": detail_card,
        }

    base_units = units[:6]
    meta_nodes = [{"id": unit.id, "title": unit.title, "detail": unit.summary[:80] or unit.detail[:80], "node_type": "meta", "layer_index": 0, "related_info_unit_id": unit.id, "preview_type": "none", "preview_url": None} for unit in base_units]
    claim_units = [unit for unit in units if unit.type == InfoUnitType.claim][:3] or (base_units[:1] if base_units else [])
    hypotheses: list[dict[str, object]] = []
    links: list[dict[str, object]] = []
    for index, unit in enumerate(claim_units, start=1):
        hyp_id = f"hyp_{index}"
        hypotheses.append({
            "id": hyp_id,
            "title": unit.title,
            "description": unit.summary[:120] or unit.detail[:120],
            "stage": _hypothesis_stage_for_unit(unit),
            "node_type": "inference",
            "layer_index": index,
            "confidence": unit.confidence.value,
            "based_on_info_unit_ids": [unit.id],
            "based_on_event_ids": [],
            "parent_hypothesis_ids": [f"hyp_{index - 1}"] if index > 1 else [],
            "selected": index == 1,
        })
        source_candidates = [item.id for item in base_units[max(0, index - 1): index + 1]] or [unit.id]
        for source_index, source_id in enumerate(source_candidates, start=1):
            links.append({"id": f"link_{index}_{source_index}", "source_id": source_id, "target_id": hyp_id, "relation": "support", "certainty": unit.confidence.value, "explanation": f"{source_id} 为该推论提供直接支撑。", "tone": "positive", "show_confidence_badge_on_click": True})
        if index > 1:
            links.append({"id": f"link_h_{index}", "source_id": f"hyp_{index - 1}", "target_id": hyp_id, "relation": "support", "certainty": unit.confidence.value, "explanation": "上一层推论与当前元信息共同推出下一层推论。", "tone": "positive", "show_confidence_badge_on_click": True})
    return {"view_type": view_type.value, "container_style": "transparent", "meta_nodes": meta_nodes, "hypotheses": hypotheses, "links": links, "groups": [], "focused_link_id": links[0]["id"] if links else None, "selected_hypothesis_id": hypotheses[0]["id"] if hypotheses else None}



def _recommended_view_to_inference_type(view_type: RecommendedView) -> InferenceType:
    if view_type == RecommendedView.conflict:
        return InferenceType.conflict
    if view_type == RecommendedView.timeline:
        return InferenceType.timeline
    return InferenceType.hypothesis



def _payload_info_unit_ids(view_payload: dict[str, object]) -> list[str]:
    if not isinstance(view_payload, dict):
        return []
    view_type = _normalize_optional_text(view_payload.get("view_type"))
    info_ids: list[str] = []
    if view_type == RecommendedView.conflict.value:
        layout_plan = view_payload.get("layout_plan")
        sections = layout_plan.get("sections") if isinstance(layout_plan, dict) else None
        if isinstance(sections, list):
            for section in sections:
                if not isinstance(section, dict):
                    continue
                cards = section.get("cards")
                if not isinstance(cards, list):
                    continue
                for item in cards:
                    if not isinstance(item, dict):
                        continue
                    card_id = _normalize_optional_text(item.get("card_id"))
                    if card_id:
                        info_ids.append(card_id)
    elif view_type == RecommendedView.timeline.value:
        events = view_payload.get("events")
        if isinstance(events, list):
            for event in events:
                if isinstance(event, dict):
                    related = event.get("related_info_unit_ids")
                    if isinstance(related, list):
                        info_ids.extend(str(item).strip() for item in related if str(item).strip())
        detail_card = view_payload.get("detail_card")
        if isinstance(detail_card, dict):
            related = detail_card.get("related_info_unit_ids")
            if isinstance(related, list):
                info_ids.extend(str(item).strip() for item in related if str(item).strip())
    else:
        meta_nodes = view_payload.get("meta_nodes")
        if isinstance(meta_nodes, list):
            for node in meta_nodes:
                if isinstance(node, dict):
                    related = _normalize_optional_text(node.get("related_info_unit_id"))
                    if related:
                        info_ids.append(related)
        hypotheses = view_payload.get("hypotheses")
        if isinstance(hypotheses, list):
            for hypothesis in hypotheses:
                if isinstance(hypothesis, dict):
                    related = hypothesis.get("based_on_info_unit_ids")
                    if isinstance(related, list):
                        info_ids.extend(str(item).strip() for item in related if str(item).strip())
    return list(dict.fromkeys(info_ids))



def _view_payload_title(case: CaseData, req: ReasoningRequest, view_payload: dict[str, object]) -> str:
    if not isinstance(view_payload, dict):
        return (req.user_prompt or case.core_issue or "Reasoning View").strip() or "Reasoning View"
    if _normalize_optional_text(view_payload.get("view_type")) == RecommendedView.timeline.value:
        timeline_title = _normalize_optional_text(view_payload.get("timeline_title"))
        if timeline_title:
            return timeline_title
    return _normalize_optional_text(view_payload.get("title")) or (req.user_prompt or case.core_issue or "Reasoning View").strip() or "Reasoning View"



def _view_payload_reasoning_steps(reason: str, view_payload: dict[str, object]) -> list[str]:
    raw_steps = view_payload.get("reasoning_steps") if isinstance(view_payload, dict) else None
    reasoning_steps = [str(step).strip() for step in raw_steps if str(step).strip()] if isinstance(raw_steps, list) else []
    return reasoning_steps or [reason]



def _create_reasoning_view_card(
    case: CaseData,
    req: ReasoningRequest,
    view_type: RecommendedView,
    reason: str,
    layout_payload: dict[str, object],
    related_meta_ids: list[str],
) -> InferenceCard:
    now = _now()
    title = _view_payload_title(case, req, layout_payload)
    reasoning_steps = _view_payload_reasoning_steps(reason, layout_payload)
    raw_resolution = layout_payload.get("recommended_resolution") if isinstance(layout_payload, dict) else None
    recommended_resolution = dict(raw_resolution) if isinstance(raw_resolution, dict) else {}
    card = InferenceCard(
        id=f"infer-{uuid4().hex[:10]}",
        title=title,
        summary=(req.user_prompt or title).strip() or title,
        display_level=DisplayLevel.level_3,
        position=CardPosition(x=520, y=220),
        source_file_ids=[],
        created_at=now,
        updated_at=now,
        inference_type=_recommended_view_to_inference_type(view_type),
        detail=InferenceDetail(
            claim=(req.user_prompt or title).strip() or title,
            view_type=view_type,
            view_payload=layout_payload,
            reasoning_steps=reasoning_steps,
            recommended_resolution=recommended_resolution,
            supporting_card_ids=related_meta_ids,
        ),
    )
    return card



async def _generate_reasoning_view_artifacts(
    case: CaseData,
    req: ReasoningRequest,
) -> tuple[ReasoningResponse, list[InferenceCard], list[GraphEdge], WorkspaceState]:
    selector_snapshot = _build_reasoning_selector_snapshot(case, req)
    selected_units, selected_relations, selected_meta_ids = _selected_reasoning_scope(case, req)
    missing_information: list[str] = []
    if not selected_units and not selected_relations:
        missing_information.append("No selected info units or relations were provided.")

    selector_request_meta: dict[str, object] = {}
    selector_response_meta: dict[str, object] = {}
    selector_raw_reply = ""
    selector_reason = ""
    selected_view = RecommendedView.hypothesis
    selector_fallback_reason = ""

    selector_user_prompt = (
        "Input payload:\n"
        + json.dumps(selector_snapshot, ensure_ascii=False, indent=2)
        + "\n\nReturn strict JSON with keys: view_type, reason."
    )
    try:
        llm_result = await asyncio.to_thread(_call_llm_sync, _get_system_prompt("reasoning_selector"), selector_user_prompt)
        selector_parsed = llm_result.get("parsed_json", {})
        selector_raw_reply = str(llm_result.get("raw_reply", ""))
        selector_request_meta = llm_result.get("request_meta", {})
        selector_response_meta = llm_result.get("response_meta", {})
        raw_view = _normalize_optional_text(selector_parsed.get("view_type")) if isinstance(selector_parsed, dict) else None
        raw_reason = _normalize_optional_text(selector_parsed.get("reason")) if isinstance(selector_parsed, dict) else None
        try:
            selected_view = RecommendedView((raw_view or "hypothesis").lower())
            selector_reason = raw_reason or "Selector chose a view without additional rationale."
            selector_status = "success"
            selector_message = "reasoning-selector-success"
        except Exception:
            selected_view, selector_reason = _fallback_reasoning_selector(case, req)
            selector_status = "invalid"
            selector_message = "reasoning-selector-fallback-invalid"
            selector_fallback_reason = "Selector returned invalid or unsupported view_type"
        append_case_log(case.case_id, "llm", {
            "stage": "reasoning_selector",
            "status": selector_status,
            "message": selector_message,
            "fallback_reason": selector_fallback_reason,
            "error_code": selector_response_meta.get("http_status"),
            "request_meta": selector_request_meta,
            "request_snapshot": selector_snapshot,
            "raw_reply": selector_raw_reply,
            "parsed_snapshot": selector_parsed,
            "response_meta": selector_response_meta,
            "selected_view": selected_view.value,
        })
    except Exception as exc:
        if isinstance(exc, LLMRequestError):
            selector_fallback_reason = str(exc)
            selector_error_code = exc.status_code
            selector_response = {"http_status": exc.status_code, "provider_body_preview": (exc.provider_body or "")[:4000]}
        else:
            selector_fallback_reason = f"Unexpected error: {exc}"
            selector_error_code = None
            selector_response = {}
        selected_view, selector_reason = _fallback_reasoning_selector(case, req)
        append_case_log(case.case_id, "llm", {
            "stage": "reasoning_selector",
            "status": "fallback",
            "message": f"llm-fallback: {exc}",
            "fallback_reason": selector_fallback_reason,
            "error_code": selector_error_code,
            "request_snapshot": selector_snapshot,
            "raw_reply": "",
            "parsed_snapshot": {},
            "response_meta": selector_response,
            "selected_view": selected_view.value,
        })

    if selected_view == RecommendedView.conflict and not req.selected_info_units and not req.selected_relations:
        conflict_req = GenerateInferenceRequest(
            case_id=case.case_id,
            selected_card_ids=[],
            mode="conflict_check",
            user_prompt=req.user_prompt,
        )
        fallback_issue = _fallback_issue_conflict_generation(case, conflict_req)
        if fallback_issue is not None:
            cards, edges, workspace, message = fallback_issue
            for card in cards:
                card.display_level = DisplayLevel.level_3
                card.decision = InferenceDecision.pending
                card.detail.view_type = RecommendedView.conflict
                card.detail.view_payload = {
                    "view_type": RecommendedView.conflict.value,
                    "title": card.title,
                    "layout_plan": {
                        "sections": [
                            {"section_id": "header", "section_type": "question_header", "title": card.title, "purpose": "Conflict issue", "placement": "top", "cards": []},
                            {"section_id": "left", "section_type": "side_column", "title": "\u76f8\u5173\u4e3b\u5f20", "purpose": "Supporting side", "placement": "left", "cards": [{"card_id": cid.removeprefix("meta-"), "card_role": "claim", "emphasis": "high", "default_expanded": True} for cid in card.detail.supporting_card_ids]},
                            {"section_id": "right", "section_type": "side_column", "title": "\u76f8\u5173\u4e3b\u5f20", "purpose": "Opposing side", "placement": "right", "cards": [{"card_id": cid.removeprefix("meta-"), "card_role": "claim", "emphasis": "high", "default_expanded": True} for cid in card.detail.opposing_card_ids]},
                        ]
                    },
                    "reasoning_steps": [
                        f"\u5f53\u524d\u4e89\u8bae\u70b9\u662f\uff1a{card.title}\u3002",
                        f"\u652f\u6301\u4fa7\u5171 {len(card.detail.supporting_card_ids)} \u6761\u4fe1\u606f\uff0c\u53cd\u5411\u4fa7\u5171 {len(card.detail.opposing_card_ids)} \u6761\u4fe1\u606f\u3002",
                        "\u53cc\u65b9\u5df2\u5f62\u6210\u53ef\u76f4\u63a5\u6bd4\u8f83\u7684\u5bf9\u7acb\u4e3b\u5f20\uff0c\u5efa\u8bae\u5148\u786e\u8ba4\u51b2\u7a81\u3002",
                    ],
                    "recommended_resolution": {
                        "recommended_decision": "accepted",
                        "preferred_side": "neutral",
                        "reason": "\u53cc\u65b9\u5df2\u56f4\u7ed5\u540c\u4e00\u4e89\u8bae\u70b9\u5f62\u6210\u53ef\u5bf9\u6bd4\u4e3b\u5f20\u3002",
                        "confidence": 0.7,
                    },
                }
                card.detail.reasoning_steps = list(card.detail.view_payload.get("reasoning_steps", []))
                card.detail.recommended_resolution = dict(card.detail.view_payload.get("recommended_resolution", {}))
            response = ReasoningResponse(
                success=True,
                recommended_view=RecommendedView.conflict,
                reasoning_structure={
                    "selected_info_units": req.selected_info_units,
                    "selected_relations": req.selected_relations,
                    "generated_cards": [card.id for card in cards],
                    "selector_reason": selector_reason,
                },
                view_payload={"view_type": RecommendedView.conflict.value, "multi_issue": True},
                missing_information=missing_information,
                reasoning_rationale=selector_reason,
                new_inference_cards=cards,
                new_edges=edges,
                message=message,
            )
            return response, cards, edges, workspace

    composer_snapshot = _build_reasoning_composer_snapshot(case, req, selected_view)
    composer_task = _reasoning_composer_task(selected_view)
    composer_user_prompt = (
        "Input payload:\n"
        + json.dumps(composer_snapshot, ensure_ascii=False, indent=2)
        + "\n\n"
        + _reasoning_composer_output_instruction(selected_view)
    )
    composer_layout = _fallback_reasoning_composer(case, req, selected_view)
    try:
        llm_result = await asyncio.to_thread(_call_llm_sync, _get_system_prompt(composer_task), composer_user_prompt)
        composer_parsed = llm_result.get("parsed_json", {})
        composer_raw_reply = str(llm_result.get("raw_reply", ""))
        composer_request_meta = llm_result.get("request_meta", {})
        composer_response_meta = llm_result.get("response_meta", {})
        if isinstance(composer_parsed, dict) and composer_parsed:
            merged_layout = dict(composer_layout)
            merged_layout.update({key: value for key, value in composer_parsed.items() if value is not None})
            merged_layout["view_type"] = selected_view.value
            if selected_view == RecommendedView.hypothesis:
                hypotheses = merged_layout.get("hypotheses")
                referenced_ids: list[str] = []
                if isinstance(hypotheses, list):
                    for idx, hypothesis in enumerate(hypotheses, start=1):
                        if isinstance(hypothesis, dict):
                            related = hypothesis.get("based_on_info_unit_ids")
                            if isinstance(related, list):
                                referenced_ids.extend(str(item).strip() for item in related if str(item).strip())
                            hypothesis.setdefault("node_type", "inference")
                            hypothesis.setdefault("layer_index", idx)
                            hypothesis.setdefault("parent_hypothesis_ids", [])
                            hypothesis.setdefault("selected", idx == 1)
                links = merged_layout.get("links")
                if isinstance(links, list):
                    for item in links:
                        if isinstance(item, dict):
                            relation = (_normalize_optional_text(item.get("relation")) or "pending").lower()
                            item["tone"] = _link_tone_for_relation(relation)
                            item["show_confidence_badge_on_click"] = True
                            source_id = _normalize_optional_text(item.get("source_id"))
                            if source_id and any(unit.id == source_id for unit in case.info_units):
                                referenced_ids.append(source_id)
                referenced_ids = list(dict.fromkeys(referenced_ids))
                if referenced_ids:
                    merged_layout["meta_nodes"] = [{"id": unit.id, "title": unit.title, "detail": unit.summary[:80] or unit.detail[:80], "node_type": "meta", "layer_index": 0, "related_info_unit_id": unit.id, "preview_type": "none", "preview_url": None} for unit in case.info_units if unit.id in referenced_ids]
                merged_layout.setdefault("container_style", "transparent")
                merged_layout.setdefault("selected_hypothesis_id", hypotheses[0].get("id") if isinstance(hypotheses, list) and hypotheses and isinstance(hypotheses[0], dict) else None)
                merged_layout["focused_link_id"] = links[0].get("id") if isinstance(links, list) and links and isinstance(links[0], dict) else None
            elif selected_view == RecommendedView.timeline:
                merged_layout.setdefault("layout_mode", "fixed_five_point")
                merged_layout.setdefault("visible_point_count", 5)
                merged_layout.setdefault("zoom_behavior", "expand_detail_on_zoom")
                timepoints = merged_layout.get("timepoints")
                if isinstance(timepoints, list):
                    for idx, timepoint in enumerate(timepoints):
                        if isinstance(timepoint, dict):
                            timepoint.setdefault("point_index", idx)
                            timepoint["emphasis"] = "focused" if idx == 0 else timepoint.get("emphasis") or "normal"
                events = merged_layout.get("events")
                first_timepoint_id = timepoints[0].get("id") if isinstance(timepoints, list) and timepoints and isinstance(timepoints[0], dict) else None
                first_event = events[0] if isinstance(events, list) and events and isinstance(events[0], dict) else None
                merged_layout["focused_timepoint_id"] = first_timepoint_id
                merged_layout["expanded_event_id"] = first_event.get("id") if first_event else None
                merged_layout["detail_card"] = {
                    "event_id": first_event.get("id"),
                    "title": first_event.get("title") or first_event.get("summary") or "元信息",
                    "detail": first_event.get("detail") or first_event.get("summary") or "",
                    "card_label": "元信息",
                    "preview_type": first_event.get("preview_type") or "none",
                    "preview_url": first_event.get("preview_url"),
                    "related_info_unit_ids": first_event.get("related_info_unit_ids") or [],
                    "source_excerpt": first_event.get("source_excerpt") or "",
                } if first_event else None
            composer_layout = merged_layout
            composer_status = "success"
            composer_message = "reasoning-composer-success"
            composer_fallback_reason = ""
        else:
            composer_status = "invalid"
            composer_message = "reasoning-composer-fallback-invalid"
            composer_fallback_reason = "Composer returned invalid payload"
        append_case_log(case.case_id, "llm", {"stage": composer_task, "status": composer_status, "message": composer_message, "fallback_reason": composer_fallback_reason, "error_code": composer_response_meta.get("http_status"), "request_meta": composer_request_meta, "request_snapshot": composer_snapshot, "raw_reply": composer_raw_reply, "parsed_snapshot": composer_parsed, "response_meta": composer_response_meta, "payload_keys": sorted(composer_layout.keys())})
    except Exception as exc:
        if isinstance(exc, LLMRequestError):
            response_meta = {"http_status": exc.status_code, "provider_body_preview": (exc.provider_body or "")[:4000]}
            error_code = exc.status_code
            fallback_reason = str(exc)
        else:
            response_meta = {}
            error_code = None
            fallback_reason = f"Unexpected error: {exc}"
        append_case_log(case.case_id, "llm", {"stage": composer_task, "status": "fallback", "message": f"llm-fallback: {exc}", "fallback_reason": fallback_reason, "error_code": error_code, "request_snapshot": composer_snapshot, "raw_reply": "", "parsed_snapshot": {}, "response_meta": response_meta, "payload_keys": sorted(composer_layout.keys())})

    referenced_info_ids = _payload_info_unit_ids(composer_layout)
    related_info_ids = list(dict.fromkeys(req.selected_info_units + [relation.source_id for relation in selected_relations] + [relation.target_id for relation in selected_relations] + referenced_info_ids))
    related_meta_ids = [f"meta-{info_id}" for info_id in related_info_ids if any(card.id == f"meta-{info_id}" for card in case.meta_cards)]
    if not related_meta_ids:
        related_meta_ids = selected_meta_ids

    card = _create_reasoning_view_card(case, req, selected_view, selector_reason, composer_layout, related_meta_ids)
    if selected_view == RecommendedView.conflict:
        sections = cast(dict[str, object], composer_layout.get("layout_plan", {})).get("sections", []) if isinstance(composer_layout.get("layout_plan"), dict) else []
        support_ids: list[str] = []
        oppose_ids: list[str] = []
        if isinstance(sections, list):
            for section in sections:
                if not isinstance(section, dict):
                    continue
                cards = section.get("cards")
                if not isinstance(cards, list):
                    continue
                ids = [f"meta-{cid}" for cid in [_normalize_optional_text(item.get("card_id")) for item in cards if isinstance(item, dict)] if cid and any(m.id == f"meta-{cid}" for m in case.meta_cards)]
                if section.get("placement") == "left":
                    support_ids.extend(ids)
                elif section.get("placement") == "right":
                    oppose_ids.extend(ids)
        card.decision = InferenceDecision.pending
        card.detail.supporting_card_ids = list(dict.fromkeys(support_ids)) or related_meta_ids[:1]
        card.detail.opposing_card_ids = [cid for cid in list(dict.fromkeys(oppose_ids)) if cid not in card.detail.supporting_card_ids]

    new_edges: list[GraphEdge] = [
        GraphEdge(id=f"edge-{uuid4().hex[:10]}", source=meta_id, target=card.id, edge_type=EdgeType.relates_to, label="reasoning_view", created_at=_now())
        for meta_id in related_meta_ids
    ]
    workspace = case.workspace_state.model_copy(deep=True)
    workspace.focused_card_id = card.id
    workspace.selected_card_ids = [card.id]
    response = ReasoningResponse(
        success=True,
        recommended_view=selected_view,
        reasoning_structure={
            "selected_info_units": req.selected_info_units,
            "selected_relations": req.selected_relations,
            "generated_cards": [card.id],
            "selector_reason": selector_reason,
        },
        view_payload=composer_layout,
        missing_information=missing_information,
        reasoning_rationale=selector_reason,
        new_inference_cards=[card],
        new_edges=new_edges,
        message="reasoning-view-generated",
    )
    return response, [card], new_edges, workspace


async def run_reasoning_agent(case: CaseData, req: ReasoningRequest) -> ReasoningResponse:
    response, cards, edges, workspace = await _generate_reasoning_view_artifacts(case, req)
    case.inference_cards.extend(cards)
    case.edges.extend(edges)
    case.workspace_state = workspace
    case.updated_at = _now()
    _append_agent_run(case, RouterTask.reasoning, "generate_reasoning_view", req.model_dump(), response.model_dump(mode="json"))
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
























