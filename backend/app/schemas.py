from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, TypeAdapter, model_validator


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



class CardType(str, Enum):
    meta = "meta"
    inference = "inference"
    law = "law"


class DisplayLevel(int, Enum):
    level_1 = 1
    level_2 = 2
    level_3 = 3


class CardStatus(str, Enum):
    active = "active"
    hidden = "hidden"
    archived = "archived"



class InferenceType(str, Enum):
    hypothesis = "hypothesis"
    conclusion = "conclusion"
    conflict = "conflict"
    timeline = "timeline"
    missing_evidence = "missing_evidence"
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
    grounded_in = "grounded_in"
    applies_to = "applies_to"
    retrieved_for = "retrieved_for"


class ViewMode(str, Enum):
    panorama = "panorama"
    reasoning = "reasoning"
    detail = "detail"


class ConfidenceLevel(str, Enum):
    high = "high"
    medium = "medium"
    low = "low"


class CertaintyLevel(str, Enum):
    explicit = "explicit"
    inferred = "inferred"


class RouterTask(str, Enum):
    extraction = "extraction"
    relation = "relation"
    reasoning = "reasoning"
    law_retrieve = "law_retrieve"
    qa = "qa"
    interaction = "interaction"


class InfoUnitType(str, Enum):
    subject = "subject"
    event = "event"
    state = "state"
    claim = "claim"


class SubjectSubtype(str, Enum):
    person = "person"
    organization = "organization"


class EventSubtype(str, Enum):
    legal_act = "legal_act"
    factual_act = "factual_act"
    transaction = "transaction"
    communication = "communication"
    violation = "violation"


class StateSubtype(str, Enum):
    temporal = "temporal"
    physical_state = "physical_state"
    usage_state = "usage_state"


class ClaimSubtype(str, Enum):
    fact_assertion = "fact_assertion"
    legal_assertion = "legal_assertion"
    defense = "defense"


class ClaimSide(str, Enum):
    landlord = "landlord"
    tenant = "tenant"
    neutral = "neutral"
    unknown = "unknown"


class SubjectLegalType(str, Enum):
    natural_person = "natural_person"
    legal_person = "legal_person"
    unincorporated_org = "unincorporated_org"


class NormSourceType(str, Enum):
    statute = "statute"
    judicial_interpretation = "judicial_interpretation"
    local_regulation = "local_regulation"
    guideline = "guideline"
    case_rule = "case_rule"
    other = "other"


class NormLevel(str, Enum):
    constitution = "constitution"
    law = "law"
    administrative_regulation = "administrative_regulation"
    local_regulation = "local_regulation"
    judicial_interpretation = "judicial_interpretation"
    normative_document = "normative_document"
    other = "other"


class InfoUnitSubtype(str, Enum):
    person = SubjectSubtype.person.value
    organization = SubjectSubtype.organization.value
    legal_act = EventSubtype.legal_act.value
    factual_act = EventSubtype.factual_act.value
    transaction = EventSubtype.transaction.value
    communication = EventSubtype.communication.value
    violation = EventSubtype.violation.value
    temporal = StateSubtype.temporal.value
    physical_state = StateSubtype.physical_state.value
    usage_state = StateSubtype.usage_state.value
    fact_assertion = ClaimSubtype.fact_assertion.value
    legal_assertion = ClaimSubtype.legal_assertion.value
    defense = ClaimSubtype.defense.value


class RecommendedView(str, Enum):
    timeline = "timeline"
    conflict = "conflict"
    hypothesis = "hypothesis"


class HypothesisStage(str, Enum):
    fact_recognition = "fact_recognition"
    legal_reasoning = "legal_reasoning"
    legal_application = "legal_application"
    final_claim = "final_claim"


class HypothesisLinkRelation(str, Enum):
    support = "support"
    attack = "attack"
    pending = "pending"


class HypothesisGroupType(str, Enum):
    AND = "AND"
    ATTACK = "ATTACK"


class HypothesisGroupStatus(str, Enum):
    complete = "complete"
    partial = "partial"


class HypothesisNodeType(str, Enum):
    meta = "meta"
    inference = "inference"


class HypothesisContainerStyle(str, Enum):
    transparent = "transparent"


class HypothesisLinkTone(str, Enum):
    positive = "positive"
    warning = "warning"
    negative = "negative"


class TimeGranularity(str, Enum):
    mixed = "mixed"
    day = "day"
    month = "month"
    year = "year"


class TimePrecision(str, Enum):
    year = "year"
    month = "month"
    day = "day"
    hour = "hour"
    unknown = "unknown"


class TimelineEventType(str, Enum):
    action = "action"
    agreement = "agreement"
    payment = "payment"
    communication = "communication"
    delivery = "delivery"
    conflict = "conflict"
    statement = "statement"
    filing = "filing"
    judgment = "judgment"
    other = "other"


class TimelinePreviewType(str, Enum):
    document = "document"
    image = "image"
    none = "none"


class TimelineConflictStatus(str, Enum):
    none = "none"
    possible_conflict = "possible_conflict"
    conflicted = "conflicted"


class TimelineLayoutMode(str, Enum):
    fixed_five_point = "fixed_five_point"


class TimelineZoomBehavior(str, Enum):
    expand_detail_on_zoom = "expand_detail_on_zoom"


class TimelinePointEmphasis(str, Enum):
    normal = "normal"
    muted = "muted"
    focused = "focused"


class LayoutSectionType(str, Enum):
    question_header = "question_header"
    side_column = "side_column"
    shared_facts = "shared_facts"
    conflict_focus = "conflict_focus"
    timeline_main = "timeline_main"
    support_panel = "support_panel"
    oppose_panel = "oppose_panel"
    pending_panel = "pending_panel"


class LayoutPlacement(str, Enum):
    top = "top"
    left = "left"
    right = "right"
    center_ = "center"
    bottom = "bottom"


class LayoutCardRole(str, Enum):
    claim = "claim"
    evidence = "evidence"
    event = "event"
    state = "state"
    summary = "summary"

class IntentType(str, Enum):
    qa = "qa"
    search = "search"
    reasoning = "reasoning"
    canvas_edit = "canvas_edit"
    view_switch = "view_switch"


class ResponseMode(str, Enum):
    text = "text"
    canvas = "canvas"
    mixed = "mixed"


class UIActionType(str, Enum):
    highlight = "highlight"
    open_view = "open_view"
    rearrange = "rearrange"
    create_node = "create_node"


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

    source_file_ids: list[str] = Field(default_factory=list)

    created_at: datetime
    updated_at: datetime


class MetaDetail(BaseModel):
    name: str | None = None
    description: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)


class MetaCard(CardBase):
    card_type: Literal[CardType.meta] = CardType.meta
    info_type: InfoUnitType
    info_subtype: InfoUnitSubtype

    detail: MetaDetail = Field(default_factory=MetaDetail)


class LawDetail(BaseModel):
    source_type: NormSourceType = NormSourceType.other
    norm_level: NormLevel = NormLevel.other
    article_no: str | None = None
    title_full: str = ""
    effective_status: str | None = None
    source_url: str | None = None
    keywords: list[str] = Field(default_factory=list)
    text: str | None = None
    source_info_unit_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _upgrade_legacy_payload(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        payload = dict(data)
        payload.setdefault("source_type", payload.get("source_type") or "other")
        payload.setdefault("norm_level", payload.get("norm_level") or "other")
        payload.setdefault("title_full", payload.get("title_full") or payload.get("legal_type") or "")
        payload.setdefault("keywords", payload.get("keywords") or [])
        return payload


class LawCard(CardBase):
    card_type: Literal[CardType.law] = CardType.law
    source_type: NormSourceType = NormSourceType.other
    norm_level: NormLevel = NormLevel.other
    detail: LawDetail

    @model_validator(mode="before")
    @classmethod
    def _upgrade_legacy_payload(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        payload = dict(data)
        raw_detail = payload.get("detail")
        detail: dict[str, Any] = raw_detail if isinstance(raw_detail, dict) else {}
        source_type = detail.get("source_type") or payload.get("source_type") or NormSourceType.other
        norm_level = detail.get("norm_level") or payload.get("norm_level") or NormLevel.other
        payload["source_type"] = source_type if isinstance(source_type, NormSourceType) else NormSourceType(str(source_type))
        payload["norm_level"] = norm_level if isinstance(norm_level, NormLevel) else NormLevel(str(norm_level))
        return payload


class RecommendedResolution(BaseModel):
    recommended_decision: InferenceDecision = InferenceDecision.pending
    preferred_side: ClaimSide = ClaimSide.neutral
    reason: str = ""
    confidence: float | None = None


class ReasoningLayoutCard(BaseModel):
    card_id: str
    card_role: LayoutCardRole
    emphasis: ConfidenceLevel = ConfidenceLevel.medium
    default_expanded: bool = False


class ReasoningLayoutSection(BaseModel):
    section_id: str
    section_type: LayoutSectionType
    title: str
    purpose: str
    placement: LayoutPlacement
    cards: list[ReasoningLayoutCard] = Field(default_factory=list)


class ReasoningLayoutPlan(BaseModel):
    sections: list[ReasoningLayoutSection] = Field(default_factory=list)


class ConflictViewPayload(BaseModel):
    view_type: Literal[RecommendedView.conflict] = RecommendedView.conflict
    title: str = ""
    layout_plan: ReasoningLayoutPlan = Field(default_factory=ReasoningLayoutPlan)
    reasoning_steps: list[str] = Field(default_factory=list)
    recommended_resolution: RecommendedResolution = Field(default_factory=RecommendedResolution)
    multi_issue: bool = False


class HypothesisMetaNode(BaseModel):
    id: str
    title: str
    detail: str
    node_type: Literal[HypothesisNodeType.meta] = HypothesisNodeType.meta
    layer_index: int = 0
    related_info_unit_id: str | None = None
    preview_type: TimelinePreviewType = TimelinePreviewType.none
    preview_url: str | None = None


class HypothesisNode(BaseModel):
    id: str
    title: str
    description: str
    stage: HypothesisStage
    node_type: Literal[HypothesisNodeType.inference] = HypothesisNodeType.inference
    layer_index: int = 1
    confidence: ConfidenceLevel = ConfidenceLevel.medium
    based_on_info_unit_ids: list[str] = Field(default_factory=list)
    based_on_event_ids: list[str] = Field(default_factory=list)
    parent_hypothesis_ids: list[str] = Field(default_factory=list)
    selected: bool = False


class HypothesisLink(BaseModel):
    id: str
    source_id: str
    target_id: str
    relation: HypothesisLinkRelation
    certainty: ConfidenceLevel = ConfidenceLevel.medium
    explanation: str
    tone: HypothesisLinkTone | None = None
    show_confidence_badge_on_click: bool = True


class HypothesisGroup(BaseModel):
    id: str
    type: HypothesisGroupType
    member_link_ids: list[str] = Field(default_factory=list)
    target_hypothesis_id: str
    status: HypothesisGroupStatus


class HypothesisViewPayload(BaseModel):
    view_type: Literal[RecommendedView.hypothesis] = RecommendedView.hypothesis
    container_style: HypothesisContainerStyle = HypothesisContainerStyle.transparent
    meta_nodes: list[HypothesisMetaNode] = Field(default_factory=list)
    hypotheses: list[HypothesisNode] = Field(default_factory=list)
    links: list[HypothesisLink] = Field(default_factory=list)
    groups: list[HypothesisGroup] = Field(default_factory=list)
    focused_link_id: str | None = None
    selected_hypothesis_id: str | None = None


class TimelineTimepoint(BaseModel):
    id: str
    time_label: str
    time_sort_value: str = ""
    time_precision: TimePrecision = TimePrecision.unknown
    event_title: str
    event_brief: str
    event_ids: list[str] = Field(default_factory=list)
    certainty: ConfidenceLevel = ConfidenceLevel.medium
    is_key_event: bool = False
    point_index: int | None = None
    emphasis: TimelinePointEmphasis = TimelinePointEmphasis.normal


class TimelineEvent(BaseModel):
    id: str
    timepoint_id: str
    title: str
    summary: str
    detail: str
    full_description: str
    actors: list[str] = Field(default_factory=list)
    location: str = ""
    event_type: TimelineEventType = TimelineEventType.other
    preview_type: TimelinePreviewType = TimelinePreviewType.none
    related_info_unit_ids: list[str] = Field(default_factory=list)
    source_excerpt: str = ""
    askable_questions: list[str] = Field(default_factory=list)
    importance: ConfidenceLevel = ConfidenceLevel.medium
    time_conflict_status: TimelineConflictStatus = TimelineConflictStatus.none
    primary_detail_info_unit_id: str | None = None
    preview_url: str | None = None


class TimelineDetailCard(BaseModel):
    event_id: str
    title: str
    detail: str
    card_label: str = "元信息"
    preview_type: TimelinePreviewType = TimelinePreviewType.none
    preview_url: str | None = None
    related_info_unit_ids: list[str] = Field(default_factory=list)
    source_excerpt: str = ""


class TimelineViewPayload(BaseModel):
    view_type: Literal[RecommendedView.timeline] = RecommendedView.timeline
    timeline_title: str = ""
    time_granularity: TimeGranularity = TimeGranularity.mixed
    layout_mode: TimelineLayoutMode = TimelineLayoutMode.fixed_five_point
    visible_point_count: int = 5
    focused_timepoint_id: str | None = None
    expanded_event_id: str | None = None
    zoom_behavior: TimelineZoomBehavior = TimelineZoomBehavior.expand_detail_on_zoom
    timepoints: list[TimelineTimepoint] = Field(default_factory=list)
    events: list[TimelineEvent] = Field(default_factory=list)
    detail_card: TimelineDetailCard | None = None


ReasoningViewPayload = ConflictViewPayload | HypothesisViewPayload | TimelineViewPayload
_REASONING_VIEW_PAYLOAD_ADAPTER = TypeAdapter(ReasoningViewPayload)


def parse_reasoning_view_payload(payload: Any) -> ReasoningViewPayload | None:
    if payload is None:
        return None
    if isinstance(payload, (ConflictViewPayload, HypothesisViewPayload, TimelineViewPayload)):
        return payload
    if not isinstance(payload, dict):
        return None
    try:
        return _REASONING_VIEW_PAYLOAD_ADAPTER.validate_python(payload)
    except Exception:
        return None

class InferenceDetail(BaseModel):
    claim: str
    view_type: RecommendedView | None = None
    view_payload: dict[str, Any] | ReasoningViewPayload = Field(default_factory=dict)
    reasoning_steps: list[str] = Field(default_factory=list)
    recommended_resolution: dict[str, Any] | RecommendedResolution = Field(default_factory=dict)

    supporting_card_ids: list[str] = Field(default_factory=list)
    opposing_card_ids: list[str] = Field(default_factory=list)
    missing_card_ids: list[str] = Field(default_factory=list)

    confidence: float | None = None
    legal_basis_ids: list[str] = Field(default_factory=list)

    @property
    def parsed_view_payload(self) -> ReasoningViewPayload | None:
        return parse_reasoning_view_payload(self.view_payload)

    @property
    def parsed_recommended_resolution(self) -> RecommendedResolution | None:
        if isinstance(self.recommended_resolution, RecommendedResolution):
            return self.recommended_resolution
        if not isinstance(self.recommended_resolution, dict):
            return None
        try:
            return RecommendedResolution.model_validate(self.recommended_resolution)
        except Exception:
            return None

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

    relation_type: str | None = None
    label: str | None = None
    weight: float | None = None

    created_at: datetime

    @model_validator(mode="after")
    def _populate_relation_type(self) -> "GraphEdge":
        if not self.relation_type:
            self.relation_type = self.label or self.edge_type.value
        return self


class InfoUnit(BaseModel):
    id: str
    type: InfoUnitType
    subtype: InfoUnitSubtype
    subject_legal_type: SubjectLegalType | None = None
    title: str
    summary: str
    detail: str
    actor: str | None = None
    target: str | None = None
    speaker: str | None = None
    side: ClaimSide | None = None
    stance_target: str | None = None
    source_refs: list[str] = Field(default_factory=list)
    confidence: ConfidenceLevel = ConfidenceLevel.medium
    extraction_reason: str
    evidence_quote: str
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="before")
    @classmethod
    def _upgrade_legacy_payload(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        payload = dict(data)
        legacy = payload.pop("legal_type", None)
        normalized = payload.get("subject_legal_type", legacy)
        if isinstance(normalized, str):
            normalized = normalized.strip().lower()
            if normalized in {"", "none", "null", "n/a", "unknown"}:
                normalized = None
        payload["subject_legal_type"] = normalized
        return payload

    @model_validator(mode="after")
    def _validate_type_subtype(self) -> "InfoUnit":
        allowed: dict[InfoUnitType, set[str]] = {
            InfoUnitType.subject: {SubjectSubtype.person.value, SubjectSubtype.organization.value},
            InfoUnitType.event: {
                EventSubtype.legal_act.value,
                EventSubtype.factual_act.value,
                EventSubtype.transaction.value,
                EventSubtype.communication.value,
                EventSubtype.violation.value,
            },
            InfoUnitType.state: {
                StateSubtype.temporal.value,
                StateSubtype.physical_state.value,
                StateSubtype.usage_state.value,
            },
            InfoUnitType.claim: {
                ClaimSubtype.fact_assertion.value,
                ClaimSubtype.legal_assertion.value,
                ClaimSubtype.defense.value,
            },
        }
        allowed_subtypes = allowed.get(self.type, set())
        if self.subtype.value not in allowed_subtypes:
            raise ValueError(
                f"Invalid subtype '{self.subtype.value}' for type '{self.type.value}'. "
                f"Allowed: {sorted(allowed_subtypes)}"
            )
        if self.type != InfoUnitType.subject and self.subject_legal_type is not None:
            raise ValueError("subject_legal_type is only allowed when type is subject")
        if self.type != InfoUnitType.event:
            self.actor = None
            self.target = None
        if self.type != InfoUnitType.claim:
            self.speaker = None
            self.side = None
            self.stance_target = None
        return self


class RelationRecord(BaseModel):
    id: str
    source_id: str
    target_id: str
    relation_type: str
    confidence: ConfidenceLevel = ConfidenceLevel.medium
    evidence_basis: str
    rationale: str
    certainty_level: CertaintyLevel = CertaintyLevel.explicit
    created_at: datetime


class AgentRunLog(BaseModel):
    run_id: str
    task: RouterTask
    subtask: str | None = None
    input_snapshot: dict[str, Any] = Field(default_factory=dict)
    output_snapshot: dict[str, Any] = Field(default_factory=dict)
    status: Literal["success", "failed", "fallback"] = "success"
    error: str | None = None
    started_at: datetime
    finished_at: datetime


class WorkspaceState(BaseModel):
    current_view: ViewMode = ViewMode.panorama

    selected_card_ids: list[str] = Field(default_factory=list)
    focused_card_id: str | None = None

    expanded_card_ids: list[str] = Field(default_factory=list)
    pinned_card_ids: list[str] = Field(default_factory=list)

    viewport: CanvasViewport = Field(default_factory=CanvasViewport)


class CaseData(BaseModel):
    case_id: str
    case_title: str = "MVP案件"

    created_at: datetime
    updated_at: datetime

    files: list[CaseFile] = Field(default_factory=list)
    info_units: list[InfoUnit] = Field(default_factory=list)
    relations: list[RelationRecord] = Field(default_factory=list)
    core_issue: str | None = None
    issue_set: list[str] = Field(default_factory=list)

    meta_cards: list[MetaCard] = Field(default_factory=list)
    inference_cards: list[InferenceCard] = Field(default_factory=list)
    law_cards: list[LawCard] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)

    workspace_state: WorkspaceState = Field(default_factory=WorkspaceState)
    agent_runs: list[AgentRunLog] = Field(default_factory=list)


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
    decision: InferenceDecision | None = None
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
    mode: Literal["auto", "hypothesis", "conclusion", "conflict_check", "missing_evidence"] = "auto"
    user_prompt: str | None = None


class GenerateInferenceResponse(BaseModel):
    success: bool
    new_inference_cards: list[InferenceCard] = Field(default_factory=list)
    new_edges: list[GraphEdge] = Field(default_factory=list)
    updated_workspace_state: WorkspaceState | None = None
    message: str | None = None


class LawRetrieveIntent(str, Enum):
    support_conflict_resolution = "support_conflict_resolution"
    retrieve_for_conclusion = "retrieve_for_conclusion"
    validate_conflict_basis = "validate_conflict_basis"


class LawRetrieveRequest(BaseModel):
    case_id: str
    conflict_card_id: str
    selected_card_ids: list[str] = Field(default_factory=list)
    intent: LawRetrieveIntent = LawRetrieveIntent.support_conflict_resolution
    query_hint: str | None = None
    max_results: int = 5


class LawRetrieveResponse(BaseModel):
    success: bool
    conflict_card_id: str
    new_law_cards: list[LawCard] = Field(default_factory=list)
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


class RouteRequest(BaseModel):
    user_input: str
    current_selection: list[str] = Field(default_factory=list)
    system_state: dict[str, Any] = Field(default_factory=dict)


class RouteResponse(BaseModel):
    task: RouterTask
    subtask: str = ""
    target_scope: list[str] = Field(default_factory=list)
    requires_tool: bool = False


class ExtractionRequest(BaseModel):
    case_id: str
    file_ids: list[str] | None = None
    raw_text: str | None = None
    context: str | None = None


class ExtractionResponse(BaseModel):
    success: bool
    info_units: list[InfoUnit] = Field(default_factory=list)
    message: str | None = None


class RelationRequest(BaseModel):
    case_id: str
    info_unit_ids: list[str] | None = None


class RelationResponse(BaseModel):
    success: bool
    relations: list[RelationRecord] = Field(default_factory=list)
    new_edges: list[GraphEdge] = Field(default_factory=list)
    message: str | None = None


class ReasoningRequest(BaseModel):
    case_id: str
    selected_info_units: list[str] = Field(default_factory=list)
    selected_relations: list[str] = Field(default_factory=list)
    user_prompt: str = ""
    current_canvas_state: dict[str, Any] = Field(default_factory=dict)


class ReasoningResponse(BaseModel):
    success: bool
    recommended_view: RecommendedView = RecommendedView.hypothesis
    reasoning_structure: dict[str, Any] = Field(default_factory=dict)
    view_payload: dict[str, Any] | ReasoningViewPayload = Field(default_factory=dict)
    missing_information: list[str] = Field(default_factory=list)
    reasoning_rationale: str = ""
    new_inference_cards: list[InferenceCard] = Field(default_factory=list)
    new_edges: list[GraphEdge] = Field(default_factory=list)
    message: str | None = None

    @property
    def parsed_view_payload(self) -> ReasoningViewPayload | None:
        return parse_reasoning_view_payload(self.view_payload)

class QARequest(BaseModel):
    case_id: str
    question: str


class QAResponse(BaseModel):
    answer: str
    matched_items: list[str] = Field(default_factory=list)
    highlight_targets: list[str] = Field(default_factory=list)
    confidence: ConfidenceLevel = ConfidenceLevel.medium


class UIAction(BaseModel):
    action: UIActionType
    targets: list[str] = Field(default_factory=list)
    params: dict[str, Any] = Field(default_factory=dict)


class InteractionRequest(BaseModel):
    case_id: str
    user_input: str
    current_selection: list[str] = Field(default_factory=list)
    system_state: dict[str, Any] = Field(default_factory=dict)


class InteractionResponse(BaseModel):
    intent_type: IntentType
    response_mode: ResponseMode
    ui_actions: list[UIAction] = Field(default_factory=list)
    assistant_message: str = ""

class InteractionTrackRequest(BaseModel):
    case_id: str
    action: str
    targets: list[str] = Field(default_factory=list)
    params: dict[str, Any] = Field(default_factory=dict)
    context: dict[str, Any] = Field(default_factory=dict)


class InteractionTrackResponse(BaseModel):
    success: bool
    run_id: str
    message: str | None = None










