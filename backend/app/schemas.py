"""Schema 定义。

本阶段（Phase 1b — Core Info Model Migration）已从旧的统一 InfoUnit 模型
迁移到 6 个 type-specific 模型：

- Person
- Event（time 是结构化 EventTime，非独立 type）
- Location
- Object
- Claim
- Evidence

extraction 输出不再有统一的 title/subtype/legal_type/side/stance_target，
也不再有 InfoUnitType/SubjectSubtype/EventSubtype/StateSubtype/ClaimSubtype
这组嵌套枚举。每个 type 只携带它需要的字段。

Reference 校验（服务层）：
- 校验通过 `CaseData.entity_by_id(ref)` 查找 + 实例类型判断完成。
- ID 前缀（person-/event-/...）只是命名约定，不作为真实性判断依据。
- 校验不通过时返回非法 id 列表，service 层记录日志但不抛错。

字段约束：
- Claim.claimant_ref 可为 None；claimant 无法解析时填 claimant_text 作 provenance。
- 只有 source 明确表示存在一个 entity（人物 / 物件 / 地点 / 事件 / 材料）时才创建该 entity。
- 不得从 Claim / 事件描述自动升级为 ownership / possession / 同一身份 等事实关系。

旧字段一律删除，不提供 compatibility layer。
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, TypeAdapter, model_validator


def _utcnow() -> datetime:
    """Server 侧时间戳默认工厂（naive UTC，与 case_service._now 保持一致）。"""
    return datetime.utcnow()


class ReferenceIntegrityError(Exception):
    """Core entity ref 完整性被破坏时抛出。

    build_structural_edges / extraction reference validation 遇到 dangling ref
    或类型不匹配时抛出，不允许静默 skip。
    """

    def __init__(self, message: str = "", invalid_refs: list[str] | None = None) -> None:
        super().__init__(message)
        self.invalid_refs = invalid_refs or []


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
    missing_evidence = "missing_evidence"
    risk = "risk"
    other = "other"


class InferenceDecision(str, Enum):
    undecided = "undecided"
    accepted = "accepted"
    rejected = "rejected"
    pending = "pending"


class GraphEdgeType(str, Enum):
    """Runtime projection 的边类型（Phase 1c）。

    覆盖两类：
    - structural（deterministic，从 core entity refs 投影，0 LLM token）：
      claimed_by / involved_in / about / occurs_in / involves_object
    - semantic（LLM 抽取，从 SemanticRelation 投影）：
      supports / contradicts（与 SemanticRelationType 共享 string value）
    """

    claimed_by = "claimed_by"
    involved_in = "involved_in"
    about = "about"
    occurs_in = "occurs_in"
    involves_object = "involves_object"
    supports = "supports"
    contradicts = "contradicts"


class SemanticRelationType(str, Enum):
    """Semantic relation 类型（仅 LLM 抽取的语义关系，Phase 1c）。

    与 GraphEdgeType 的 supports/contradicts 共享 string value，但二者是独立枚举：
    SemanticRelation.relation_type 在类型层就不可能接受 claimed_by/involved_in 等
    structural edge type。
    """

    supports = "supports"
    contradicts = "contradicts"


class ViewMode(str, Enum):
    panorama = "panorama"
    reasoning = "reasoning"
    detail = "detail"


class ConfidenceLevel(str, Enum):
    high = "high"
    medium = "medium"
    low = "low"


class RouterTask(str, Enum):
    extraction = "extraction"
    relation = "relation"
    reasoning = "reasoning"
    qa = "qa"
    interaction = "interaction"


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


class TimePrecision(str, Enum):
    year = "year"
    month = "month"
    day = "day"
    hour = "hour"
    unknown = "unknown"


class TimelinePreviewType(str, Enum):
    """HypothesisMetaNode.preview_type 仍在复用，暂保留。"""

    document = "document"
    image = "image"
    none = "none"


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


# ---------------------------------------------------------------------------
# Core Info Model — Phase 1b 新 schema
# ---------------------------------------------------------------------------

EventTimePrecision = Literal["exact", "approximate", "unknown"]


class EventTime(BaseModel):
    """Event / Claim 共用的时间属性。结构化对象，不作为独立 InfoUnit type。

    字段语义：

    - `label`：source 中用户可读的时间表达，忠实原文，**不要求机器可排序**。
    - `sort_value`：canonical temporal key。`!= None` 表示"可以直接机器比较的
      absolute canonical time"，格式统一为 `YYYY-MM-DDTHH:MM`。
      仅在 source / extraction context 足以可靠得到**完整年月日 + 时间**时生成；
      不得复制 `label`，不得猜测 source 中不存在的年 / 月 / 日 / 相对时间锚点。
      无法可靠规范化时必须为 `null`，该情况下该项不参与时间排序。
      （partial key 如 `MM-DDTHH:MM`、`HH:MM` 一律不使用；relative time
      resolution 与 partial temporal ordering 不在本 contract 范围内。）
    - `precision`：source 时间**陈述的确定程度**，取值 `exact / approximate / unknown`。
      它**不**表示 year/month/day/hour/minute 粒度。
    """

    label: str
    sort_value: str | None = None
    precision: EventTimePrecision = "unknown"


class Person(BaseModel):
    id: str
    name: str
    # Phase 1c：仅 schema support。aliases 不生成 same_as edge，本阶段不实现
    # 任何 LLM entity resolution / alias merge 行为（留给后续 Extraction / Entity
    # Resolution contract 设计）。
    aliases: list[str] = Field(default_factory=list)
    description: str | None = None
    source_refs: list[str] = Field(default_factory=list)
    confidence: ConfidenceLevel = ConfidenceLevel.medium
    evidence_quote: str = ""

    # created_at / updated_at 带默认工厂：Extraction LLM 直接复用本模型输出，
    # 无需在 LLM 契约中携带时间戳（server 侧统一补写）。
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class Location(BaseModel):
    id: str
    name: str
    description: str | None = None
    source_refs: list[str] = Field(default_factory=list)
    confidence: ConfidenceLevel = ConfidenceLevel.medium
    evidence_quote: str = ""

    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class Object(BaseModel):
    id: str
    name: str
    description: str | None = None
    source_refs: list[str] = Field(default_factory=list)
    confidence: ConfidenceLevel = ConfidenceLevel.medium
    evidence_quote: str = ""

    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class Event(BaseModel):
    id: str
    description: str
    time: EventTime | None = None
    # Phase 1c：actor_ref → participant_refs（一个 Event 可有多个参与者）。
    participant_refs: list[str] = Field(default_factory=list)
    location_ref: str | None = None
    object_refs: list[str] = Field(default_factory=list)
    source_refs: list[str] = Field(default_factory=list)
    confidence: ConfidenceLevel = ConfidenceLevel.medium
    evidence_quote: str = ""

    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class Claim(BaseModel):
    """陈述/观点。

    - source ≠ claimant：claimant_ref 指向 Person，但 source_refs 指向文件证据。
    - claimant_ref 可为 None：source 只说"某人说了X"但未指名时，
      用 claimant_text 保留原文 provenance，不做身份推断或自动匿名 Person 创建。

    Claim 不是"低 confidence Event"：claimant 描述了某个事件，也不额外生成 Event。
    Claim 描述内容可携带 proposition 的强类型结构化描述（time / participant_refs /
    location_ref / object_refs）；这些字段全部可选——并非所有 Claim 都描述事件
    （例如"这把刀是李明的"是合法 Claim，不需要 time）。

    about_refs vs 强类型字段：
    - about_refs：Claim 广义上涉及/谈论的实体（任意 kind）。
    - participant_refs / location_ref / object_refs / time：对 proposition 的强类型结构化描述。
    二者允许重叠，本轮不做收敛。
    """

    id: str
    # Phase 1c：speaker_ref → claimant_ref。
    claimant_ref: str | None = None
    # Phase 1c：speaker_text → claimant_text。
    claimant_text: str | None = None
    proposition: str
    about_refs: list[str] = Field(default_factory=list)

    # proposition 内容的强类型结构化描述（均可选：Claim 不必描述事件）。
    # 复用 EventTime（不新建时间类型）：label / sort_value / precision。
    # Event.time 与 Claim.time 共用同一 contract。
    time: EventTime | None = None
    # 陈述内容涉及的人（不含仅作为说话者的 claimant，除非 claimant 本身也是 proposition 参与者）。
    participant_refs: list[str] = Field(default_factory=list)
    # 陈述内容涉及的地点。
    location_ref: str | None = None
    # 陈述内容涉及的物体。
    object_refs: list[str] = Field(default_factory=list)

    source_refs: list[str] = Field(default_factory=list)
    confidence: ConfidenceLevel = ConfidenceLevel.medium
    evidence_quote: str = ""

    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class Evidence(BaseModel):
    """材料/记录本身。只描述材料记录了什么，不代表内容真假。"""

    id: str
    description: str
    about_refs: list[str] = Field(default_factory=list)
    source_refs: list[str] = Field(default_factory=list)
    confidence: ConfidenceLevel = ConfidenceLevel.medium
    evidence_quote: str = ""

    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


# 所有 entity 类型的 union，供 CaseData 校验 reference 时遍历。
CoreEntity = Person | Event | Location | Object | Claim | Evidence


class ExtractionLLMResult(BaseModel):
    """Phase 2：Extraction LLM 直接返回的 typed 输出契约。

    只允许 6 类 entity 集合。`extra="forbid"` 显式拒绝 legacy 字段
    （info_units / core_issue / issue_set 等），确保 pipeline 不再经过旧 schema。

    LLM 侧只需输出每个 entity 的 id 与业务字段；created_at / updated_at 由
    server 侧统一补写（见各 entity 的 default_factory）。
    """

    model_config = {"extra": "forbid"}

    persons: list[Person] = Field(default_factory=list)
    events: list[Event] = Field(default_factory=list)
    locations: list[Location] = Field(default_factory=list)
    objects: list[Object] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)
    evidences: list[Evidence] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 引用校验：在 service 层用 core_entity_by_id 表做跨实体校验，
# 单独的 schema validator 只能看单 entity 内的 ref 格式（id 非空字符串）。
# ---------------------------------------------------------------------------


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


# MetaDetail 现在不再承载 info_type/info_subtype；保留 description/tags/attributes
# 以便前端展示与未来扩展，但不参与本阶段 type 推断。
class MetaDetail(BaseModel):
    name: str | None = None
    description: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)


# 实体类型 tag，用于卡片渲染与前端 kindOf 判定。
# 不携带"法律分类"语义，仅作 display tag。
class MetaEntityKind(str, Enum):
    person = "person"
    event = "event"
    location = "location"
    object = "object"
    claim = "claim"
    evidence = "evidence"


class MetaCard(CardBase):
    """对应一个 core entity 的展示卡。

    `entity_ref` 指向具体 entity id（person-xxx / event-xxx / claim-xxx ...），
    `entity_kind` 仅供前端按 kind 渲染（person/event/...）。两字段必须一致。
    """

    card_type: Literal[CardType.meta] = CardType.meta
    entity_kind: MetaEntityKind
    entity_ref: str

    detail: MetaDetail = Field(default_factory=MetaDetail)

    @model_validator(mode="after")
    def _validate_entity_prefix(self) -> "MetaCard":
        """entity_ref id 前缀应与 entity_kind 匹配，仅做格式提示，不强制。"""
        expected_prefix = {
            MetaEntityKind.person: "person-",
            MetaEntityKind.event: "event-",
            MetaEntityKind.location: "location-",
            MetaEntityKind.object: "object-",
            MetaEntityKind.claim: "claim-",
            MetaEntityKind.evidence: "evidence-",
        }.get(self.entity_kind, "")
        if expected_prefix and not self.entity_ref.startswith(expected_prefix):
            # 不抛错，只在 attributes 里记录；具体 entity 校验在 service 层。
            self.detail.attributes.setdefault("entity_ref_prefix_hint", expected_prefix)
        return self


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


# Phase 1b.1：删除 RecommendedResolution.preferred_side。
# 旧 side 枚举（ClaimSide）已删除，立场归类应在 Relation 阶段（Phase 1c）由
# entity-aware Relation 抽取完成；本阶段不允许在 schema 暴露任何 side/position 字段，
# 也禁止 service 层用 "neutral"/"landlord"/"tenant" 等自由 str 充当 side。
class RecommendedResolution(BaseModel):
    model_config = {"extra": "forbid"}

    recommended_decision: InferenceDecision = InferenceDecision.pending
    reason: str = ""
    confidence: float | None = None


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


# ---------------------------------------------------------------------------
# Timeline View Model（runtime projection，不持久化、不建事实实体）
#
# Timeline 只消费 Core Entity（Event / Claim）的 time，通过 item_ref 回查。
# 不复制 actors / location 文本 / summary / event_type 等 Core Entity 字段。
# 不写入 CaseData：每次请求由 build_timeline(case) 现场投影。
# ---------------------------------------------------------------------------


class TimelineItemType(str, Enum):
    event = "event"
    claim = "claim"


class TimelineItem(BaseModel):
    item_type: TimelineItemType
    # 指向 Core Entity id（event-* / claim-*）。前端据此回查 CaseData。
    item_ref: str
    time: EventTime


class TimelineView(BaseModel):
    case_id: str
    # 已按确定性规则排序（见 case_service.build_timeline）。
    items: list[TimelineItem] = Field(default_factory=list)


ReasoningViewPayload = ConflictViewPayload | HypothesisViewPayload
_REASONING_VIEW_PAYLOAD_ADAPTER = TypeAdapter(ReasoningViewPayload)


def parse_reasoning_view_payload(payload: Any) -> ReasoningViewPayload | None:
    if payload is None:
        return None
    if isinstance(payload, (ConflictViewPayload, HypothesisViewPayload)):
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
    """Runtime projection 的边（Phase 1c，不持久化）。

    由 `build_structural_edges` / `build_semantic_edges` 在运行时从 core entity refs
    与 semantic_relations 投影生成，不写入 CaseData。
    """

    id: str
    source: str
    target: str
    edge_type: GraphEdgeType

    relation_type: str | None = None
    label: str | None = None
    weight: float | None = None

    created_at: datetime

    @model_validator(mode="after")
    def _populate_relation_type(self) -> "GraphEdge":
        if not self.relation_type:
            self.relation_type = self.label or self.edge_type.value
        return self


class SemanticRelation(BaseModel):
    """语义关系（Phase 1c，持久化）。

    仅由 Relation stage 的 LLM 抽取产生，描述两个 information item 之间是否存在
    supports / contradicts 语义关系。端点约束见 run_relation_agent。

    - relation_type: SemanticRelationType（类型层禁止 structural edge type）。
    - detection_confidence: 模型对「这两个 item 之间存在该语义关系」的判断置信度，
      不同于 Core Entity 的 confidence（extraction confidence）。
    - rationale: 一句说明该关系为何成立。
    """

    id: str
    source_id: str
    target_id: str
    relation_type: SemanticRelationType
    detection_confidence: ConfidenceLevel = ConfidenceLevel.medium
    rationale: str = ""
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

    # 旧统一 info_units 替换为 6 个 entity 集合。
    persons: list[Person] = Field(default_factory=list)
    events: list[Event] = Field(default_factory=list)
    locations: list[Location] = Field(default_factory=list)
    objects: list[Object] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)
    evidences: list[Evidence] = Field(default_factory=list)

    # Phase 1c：semantic relations 持久化；structural GraphEdge 不持久化（runtime projection）。
    semantic_relations: list[SemanticRelation] = Field(default_factory=list)

    meta_cards: list[MetaCard] = Field(default_factory=list)
    inference_cards: list[InferenceCard] = Field(default_factory=list)

    workspace_state: WorkspaceState = Field(default_factory=WorkspaceState)
    agent_runs: list[AgentRunLog] = Field(default_factory=list)

    def entity_by_id(self, entity_id: str) -> CoreEntity | None:
        for collection in (self.persons, self.events, self.locations, self.objects, self.claims, self.evidences):
            for entity in collection:
                if entity.id == entity_id:
                    return entity
        return None

    def all_entities(self) -> list[CoreEntity]:
        return [
            *self.persons,
            *self.events,
            *self.locations,
            *self.objects,
            *self.claims,
            *self.evidences,
        ]


class GetCaseResponse(BaseModel):
    case: CaseData | None = None


class GetTimelineResponse(BaseModel):
    """Timeline runtime projection 响应。

    Timeline 不持久化：每次请求由 build_timeline(case) 现场投影。
    """

    success: bool = True
    timeline: TimelineView | None = None


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
    persons: list[Person] = Field(default_factory=list)
    events: list[Event] = Field(default_factory=list)
    locations: list[Location] = Field(default_factory=list)
    objects: list[Object] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)
    evidences: list[Evidence] = Field(default_factory=list)
    message: str | None = None


class RelationRequest(BaseModel):
    case_id: str
    info_unit_ids: list[str] | None = None


class RelationResponse(BaseModel):
    success: bool
    semantic_relations: list[SemanticRelation] = Field(default_factory=list)
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
