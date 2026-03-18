"""Pydantic schemas for request/response validation."""

from typing import Any, Literal

from pydantic import BaseModel, Field


EvidenceType = Literal["text", "document", "image", "video", "audio"]
EntityType = Literal["person", "location", "organization", "object", "account", "time"]
StanceType = Literal["support", "oppose", "neutral"]
StatusType = Literal["high", "medium", "low", "unknown"]


class EvidenceInput(BaseModel):
    """Unified evidence payload accepted by the backend."""

    id: str | None = None
    type: EvidenceType
    name: str = Field(..., min_length=1)
    content: str = Field(default="")
    file_name: str | None = None
    mime_type: str | None = None
    notes: str | None = None


class ParsedEvidence(BaseModel):
    """Normalized evidence text produced by type-specific tools."""

    id: str
    type: EvidenceType
    name: str
    parser_tool: str
    normalized_text: str
    metadata: dict = Field(default_factory=dict)


class EvidenceItem(BaseModel):
    """Original evidence unit used for downstream reasoning."""

    id: str
    type: EvidenceType
    original_content: str = ""
    source_file: str = ""
    page_or_paragraph: str = ""
    time: str = ""
    producer_or_speaker: str = ""
    is_original_evidence: bool = True
    notes: str = ""


class Entity(BaseModel):
    """Named entity extracted from evidence."""

    id: str
    name: str
    type: EntityType
    aliases: list[str] = Field(default_factory=list)
    source_evidence_ids: list[str] = Field(default_factory=list)


class Relation(BaseModel):
    """Typed relation between two entities."""

    id: str
    subject_entity: str
    object_entity: str
    relation_type: str
    time: str = ""
    evidence_sources: list[str] = Field(default_factory=list)
    confidence_status: StatusType = "unknown"


class Event(BaseModel):
    """Structured event extracted from evidence."""

    id: str
    event_type: str
    participant_entities: list[str] = Field(default_factory=list)
    time: str = ""
    location: str = ""
    description: str = ""
    source_evidence_ids: list[str] = Field(default_factory=list)


class Claim(BaseModel):
    """Claim or allegation grounded in evidence."""

    id: str
    content: str
    source: str = ""
    target_ids: list[str] = Field(default_factory=list)
    stance: StanceType = "neutral"
    credibility_status: StatusType = "unknown"
    quote: str = ""


class LLMLog(BaseModel):
    """Diagnostic information about one model invocation."""

    provider: str = "github_models"
    model: str = ""
    endpoint: str = ""
    llm_used: bool = False
    fallback_reason: str = ""
    prompt_system: str = ""
    prompt_user: str = ""
    raw_response: dict = Field(default_factory=dict)
    raw_content: str = ""
    usage: dict = Field(default_factory=dict)
    limits: dict = Field(default_factory=dict)
    error: str = ""


class StageLog(BaseModel):
    """Diagnostic information for one pipeline stage."""

    stage_name: str
    llm_used: bool = False
    fallback_used: bool = False
    fallback_reason: str = ""
    prompt_system: str = ""
    prompt_user: str = ""
    raw_response: dict[str, Any] = Field(default_factory=dict)
    raw_content: str = ""
    usage: dict[str, Any] = Field(default_factory=dict)
    limits: dict[str, Any] = Field(default_factory=dict)
    error: str = ""


class PipelineLog(BaseModel):
    """Aggregated pipeline diagnostics across multiple stages."""

    provider: str = "github_models"
    model: str = ""
    endpoint: str = ""
    pipeline_llm_used: bool = False
    fallback_reason: str = ""
    stages: list[StageLog] = Field(default_factory=list)


class ReasonRequest(BaseModel):
    """Input payload for reasoning endpoint."""

    case_text: str = Field(..., min_length=1)
    question: str = Field(..., min_length=1)
    evidences: list[EvidenceInput] = Field(default_factory=list)


class CaseCreateRequest(BaseModel):
    """Payload used to create or replace the current case."""

    case_text: str = ""
    evidences: list[EvidenceInput] = Field(default_factory=list)


class QuestionReasonRequest(BaseModel):
    """Payload used to ask a question against the current case."""

    question: str = Field(..., min_length=1)


class ExtractionStageResponse(BaseModel):
    """Stage 1 output: extracted case structure from evidence."""

    entities: list[Entity] = Field(default_factory=list)
    relations: list[Relation] = Field(default_factory=list)
    events: list[Event] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)
    stage_log: StageLog = Field(default_factory=lambda: StageLog(stage_name="extraction"))


class QuestionReasoningStageResponse(BaseModel):
    """Stage 2+3 output: reasoning graph and interface recommendation for a question."""

    question: str = ""
    conflicts: list[dict[str, Any]] = Field(default_factory=list)
    evidence_paths: list[dict[str, Any]] = Field(default_factory=list)
    recommended_view: str = "conflict_compare"
    summary: str = ""
    stage_log: StageLog = Field(default_factory=lambda: StageLog(stage_name="question_reasoning"))


class CurrentCase(BaseModel):
    """The single current case stored by the backend."""

    case_id: str = ""
    case_text: str = ""
    parsed_evidences: list[ParsedEvidence] = Field(default_factory=list)
    evidence_items: list[EvidenceItem] = Field(default_factory=list)
    entities: list[Entity] = Field(default_factory=list)
    relations: list[Relation] = Field(default_factory=list)
    events: list[Event] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)
    extraction_log: PipelineLog = Field(default_factory=PipelineLog)


class CaseCreateResponse(BaseModel):
    """Response returned after creating or replacing the current case."""

    case: CurrentCase = Field(default_factory=CurrentCase)


class CaseQuestionResponse(BaseModel):
    """Response returned after asking a question against the current case."""

    case_id: str = ""
    question: str = ""
    conflicts: list[dict[str, Any]] = Field(default_factory=list)
    evidence_paths: list[dict[str, Any]] = Field(default_factory=list)
    recommended_view: str = "conflict_compare"
    summary: str = ""
    reasoning_log: PipelineLog = Field(default_factory=PipelineLog)


class CurrentCaseEnvelope(BaseModel):
    """Wrapper used by GET /api/case."""

    case: CurrentCase | None = None


class ReasonResponse(BaseModel):
    """Structured reasoning output consumed by frontend."""

    llm_used: bool = False
    fallback_reason: str = ""
    llm_log: LLMLog = Field(default_factory=LLMLog)
    parsed_evidences: list[ParsedEvidence] = Field(default_factory=list)
    evidence_items: list[EvidenceItem] = Field(default_factory=list)
    entities: list[Entity] = Field(default_factory=list)
    relations: list[Relation] = Field(default_factory=list)
    events: list[Event] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)
    conflicts: list[dict] = Field(default_factory=list)
    evidence_paths: list[dict] = Field(default_factory=list)
    recommended_view: str = "conflict_compare"
    summary: str = ""
