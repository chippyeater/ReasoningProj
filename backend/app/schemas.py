from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class FileType(str, Enum):
    pdf = "pdf"
    docx = "docx"
    image = "image"
    txt = "txt"
    markdown = "markdown"
    other = "other"


class FileParseStatus(str, Enum):
    pending = "pending"
    parsing = "parsing"
    parsed = "parsed"
    failed = "failed"


class EvidenceType(str, Enum):
    text_span = "text_span"
    image_region = "image_region"
    screenshot = "screenshot"
    table = "table"
    extracted_statement = "extracted_statement"
    other = "other"


class CardType(str, Enum):
    meta = "meta"
    inference = "inference"


class DisplayLevel(int, Enum):
    level_1 = 1
    level_2 = 2
    level_3 = 3


class CardStatus(str, Enum):
    active = "active"
    hidden = "hidden"
    archived = "archived"


class MetaType(str, Enum):
    person = "person"
    organization = "organization"
    location = "location"
    time = "time"
    object = "object"
    account = "account"
    document = "document"
    event = "event"
    claim = "claim"
    law = "law"
    other = "other"


class InferenceType(str, Enum):
    hypothesis = "hypothesis"
    conclusion = "conclusion"
    conflict = "conflict"
    missing_evidence = "missing_evidence"
    reasoning_step = "reasoning_step"
    evidence_chain = "evidence_chain"
    risk = "risk"
    other = "other"


class InferenceDecision(str, Enum):
    undecided = "undecided"
    accepted = "accepted"
    rejected = "rejected"
    pending = "pending"


class EdgeType(str, Enum):
    relates_to = "relates_to"
    supports = "supports"
    opposes = "opposes"
    derives = "derives"
    mentions = "mentions"
    conflicts_with = "conflicts_with"
    missing_for = "missing_for"
    cites = "cites"
    belongs_to = "belongs_to"


class ViewMode(str, Enum):
    panorama = "panorama"
    reasoning = "reasoning"
    detail = "detail"


class CaseFile(BaseModel):
    file_id: str
    filename: str
    file_type: FileType
    file_size: int | None = None
    storage_path: str | None = None
    uploaded_at: datetime
    parse_status: FileParseStatus = FileParseStatus.pending

    preview_text: str | None = None
    page_count: int | None = None
    error_message: str | None = None


class EvidenceAnchor(BaseModel):
    file_id: str
    page: int | None = None
    section: str | None = None
    paragraph_index: int | None = None
    char_start: int | None = None
    char_end: int | None = None
    bbox: list[float] | None = None


class EvidenceItem(BaseModel):
    evidence_id: str
    evidence_type: EvidenceType = EvidenceType.text_span

    label: str
    content: str | None = None
    summary: str | None = None

    anchors: list[EvidenceAnchor] = Field(default_factory=list)
    source_file_ids: list[str] = Field(default_factory=list)

    created_at: datetime


class CardPosition(BaseModel):
    x: float = 0
    y: float = 0


class CardUIState(BaseModel):
    selected: bool = False
    highlighted: bool = False
    pinned: bool = False
    collapsed: bool = False


class CanvasViewport(BaseModel):
    zoom: float = 1.0
    offset_x: float = 0
    offset_y: float = 0


class CardBase(BaseModel):
    id: str
    card_type: CardType

    title: str
    summary: str | None = None

    display_level: DisplayLevel = DisplayLevel.level_1
    status: CardStatus = CardStatus.active

    position: CardPosition = Field(default_factory=CardPosition)
    ui_state: CardUIState = Field(default_factory=CardUIState)

    source_evidence_ids: list[str] = Field(default_factory=list)
    source_file_ids: list[str] = Field(default_factory=list)

    created_at: datetime
    updated_at: datetime


class MetaDetail(BaseModel):
    name: str | None = None
    aliases: list[str] = Field(default_factory=list)

    description: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)

    image_urls: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class MetaCard(CardBase):
    card_type: Literal[CardType.meta] = CardType.meta
    meta_type: MetaType

    detail: MetaDetail = Field(default_factory=MetaDetail)


class InferenceDetail(BaseModel):
    claim: str
    reasoning_steps: list[str] = Field(default_factory=list)

    supporting_card_ids: list[str] = Field(default_factory=list)
    opposing_card_ids: list[str] = Field(default_factory=list)
    missing_card_ids: list[str] = Field(default_factory=list)

    confidence: float | None = None
    legal_basis_ids: list[str] = Field(default_factory=list)

    notes: str | None = None


class InferenceCard(CardBase):
    card_type: Literal[CardType.inference] = CardType.inference
    inference_type: InferenceType

    decision: InferenceDecision = InferenceDecision.undecided
    detail: InferenceDetail


class GraphEdge(BaseModel):
    id: str
    source: str
    target: str
    edge_type: EdgeType

    label: str | None = None
    weight: float | None = None

    source_evidence_ids: list[str] = Field(default_factory=list)
    created_at: datetime


class WorkspaceState(BaseModel):
    current_view: ViewMode = ViewMode.panorama

    selected_card_ids: list[str] = Field(default_factory=list)
    focused_card_id: str | None = None

    expanded_card_ids: list[str] = Field(default_factory=list)
    pinned_card_ids: list[str] = Field(default_factory=list)

    viewport: CanvasViewport = Field(default_factory=CanvasViewport)


class CaseData(BaseModel):
    case_id: str
    case_title: str = "未命名案件"

    created_at: datetime
    updated_at: datetime

    files: list[CaseFile] = Field(default_factory=list)
    evidences: list[EvidenceItem] = Field(default_factory=list)

    meta_cards: list[MetaCard] = Field(default_factory=list)
    inference_cards: list[InferenceCard] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)

    workspace_state: WorkspaceState = Field(default_factory=WorkspaceState)


class GetCaseResponse(BaseModel):
    case: CaseData | None = None


class UpsertCaseResponse(BaseModel):
    success: bool
    case: CaseData
    message: str | None = None


class UpdateCardRequest(BaseModel):
    title: str | None = None
    summary: str | None = None
    display_level: DisplayLevel | None = None
    position: CardPosition | None = None
    ui_state: CardUIState | None = None
    detail: dict[str, Any] | None = None


class UpdateWorkspaceRequest(BaseModel):
    current_view: ViewMode | None = None
    selected_card_ids: list[str] | None = None
    focused_card_id: str | None = None
    expanded_card_ids: list[str] | None = None
    pinned_card_ids: list[str] | None = None
    viewport: CanvasViewport | None = None



class UpdateCaseRequest(BaseModel):
    case_title: str

class GenerateInferenceRequest(BaseModel):
    case_id: str
    selected_card_ids: list[str]
    mode: Literal["hypothesis", "conclusion", "conflict_check", "missing_evidence"] = "hypothesis"
    user_prompt: str | None = None


class GenerateInferenceResponse(BaseModel):
    success: bool
    new_inference_cards: list[InferenceCard] = Field(default_factory=list)
    new_edges: list[GraphEdge] = Field(default_factory=list)
    updated_workspace_state: WorkspaceState | None = None
    message: str | None = None


class CaseListItem(BaseModel):
    case_id: str
    case_title: str
    updated_at: datetime
    is_current: bool = False


class ListCasesResponse(BaseModel):
    cases: list[CaseListItem] = Field(default_factory=list)
    current_case_id: str | None = None

