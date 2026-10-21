"""The complete LogicWeaver MVP v0.3 data contract.

Transport envelopes live in api_models.py. No input upgrades or field aliases.
"""
from __future__ import annotations

from enum import Enum
from pathlib import PurePosixPath
from typing import Annotated, Generic, Literal, TypeVar

from fastapi import UploadFile
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, field_validator, model_validator


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


Text = Annotated[str, Field(min_length=1, pattern=r"\S")]
Title = Annotated[str, Field(min_length=2, max_length=24)]
Content = Annotated[str, Field(min_length=4, max_length=220)]
Index = Annotated[StrictInt, Field(ge=0)]


class SourceKind(str, Enum):
    document = "document"
    communication = "communication"
    testimony = "testimony"
    record = "record"
    other = "other"


class UploadRequest(ContractModel):
    case_title: Text
    files: Annotated[list[UploadFile], Field(min_length=1)]
    source_kinds: dict[Text, SourceKind]


def unique(values: list[str], field: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{field}: duplicate values")


def relative_path(value: str, directory: str) -> None:
    path = PurePosixPath(value)
    if ("\\" in value or ":" in value or path.is_absolute()
            or ".." in path.parts or len(path.parts) != 2
            or path.parts[0] != directory or str(path) != value):
        raise ValueError(f"path must be a relative {directory}/filename path")


class FileType(str, Enum):
    pdf = "pdf"
    docx = "docx"
    txt = "txt"
    markdown = "markdown"
    image = "image"
    other = "other"


class ParseStatus(str, Enum):
    parsed = "parsed"
    failed = "failed"
    unsupported = "unsupported"


class CaseFile(ContractModel):
    file_id: Text
    filename: Text
    file_type: FileType
    source_kind: SourceKind
    file_size: Index
    original_path: Text
    parsed_text_path: Text | None
    parse_status: ParseStatus
    error_message: Text | None
    uploaded_at: Text

    @model_validator(mode="after")
    def check_parse_result(self) -> CaseFile:
        relative_path(self.original_path, "upload_files")
        if self.parse_status == ParseStatus.parsed:
            if self.parsed_text_path is None or self.error_message is not None:
                raise ValueError("parsed files require a text path and no error")
            relative_path(self.parsed_text_path, "parsed")
            if not self.parsed_text_path.endswith(".txt"):
                raise ValueError("parsed text must be a TXT file")
        elif self.parsed_text_path is not None or self.error_message is None:
            raise ValueError("failed/unsupported files require an error and no text path")
        return self


class ParsedDocument(ContractModel):
    file_id: Text
    text: Text


class ExtractionFile(ContractModel):
    file_id: Text
    filename: Text
    text: Text


class ExtractionInput(ContractModel):
    case_title: Text
    files: list[ExtractionFile]


class InfoUnitType(str, Enum):
    entity = "entity"
    event = "event"
    state = "state"
    rule = "rule"
    position = "position"


class InfoUnitSubtype(str, Enum):
    person = "person"
    organization = "organization"
    object = "object"
    location = "location"
    reported = "reported"
    documented = "documented"
    verified = "verified"


def check_info_classification(info_type: InfoUnitType | str, subtype: InfoUnitSubtype | str | None,
                              *, allow_verified: bool = True) -> None:
    allowed = {
        "entity": {"person", "organization", "object", "location"},
        "event": {"reported", "documented", "verified", None},
        "state": {None},
        "rule": {None},
        "position": {None},
    }
    type_value = info_type.value if isinstance(info_type, InfoUnitType) else info_type
    value = subtype.value if isinstance(subtype, InfoUnitSubtype) else subtype
    if type_value not in allowed or value not in allowed[type_value] or (value == "verified" and not allow_verified):
        raise ValueError("subtype does not belong to type")


class SourceRef(ContractModel):
    file_id: Text
    source_kind: SourceKind
    quote: Text
    start: Index
    end: Index

    @model_validator(mode="after")
    def check_range(self) -> SourceRef:
        if self.end <= self.start or self.end - self.start != len(self.quote):
            raise ValueError("source range must match the Unicode length of quote")
        return self


class InfoUnit(ContractModel):
    id: Text
    type: InfoUnitType
    subtype: InfoUnitSubtype | None
    title: Title
    content: Content
    source_refs: Annotated[list[SourceRef], Field(min_length=1)]
    speaker: Text | None
    side: Text | None
    actor: Text | None
    target: Text | None
    related_issue: Text | None
    created_at: Text

    @field_validator("title", "content")
    @classmethod
    def reject_surrounding_whitespace(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("text must not have leading or trailing whitespace")
        return value

    @model_validator(mode="after")
    def check_classification(self) -> InfoUnit:
        check_info_classification(self.type, self.subtype)
        if self.type in {InfoUnitType.state, InfoUnitType.position} and self.speaker is None:
            raise ValueError("state and position require a nonempty speaker")
        return self


class ExtractionOutput(ContractModel):
    core_issue: Text | None
    issue_set: list[Text]
    info_units: list[InfoUnit]

    @model_validator(mode="after")
    def check_issues(self) -> ExtractionOutput:
        unique(self.issue_set, "issue_set")
        unique([u.id for u in self.info_units], "info_units.id")
        if self.core_issue is not None and self.core_issue not in self.issue_set:
            raise ValueError("core_issue must occur in issue_set")
        if any(u.related_issue is not None and u.related_issue not in self.issue_set
               for u in self.info_units):
            raise ValueError("related_issue must occur in issue_set")
        lookup = {u.id: u for u in self.info_units}
        for unit in self.info_units:
            for field in ("actor", "target"):
                target_id = getattr(unit, field)
                if target_id is not None and (target_id not in lookup or lookup[target_id].type != InfoUnitType.entity):
                    raise ValueError(f"{unit.id}.{field} must reference an entity InfoUnit")
        return self


class ValidationInput(ContractModel):
    extraction: ExtractionOutput
    parsed_files: list[CaseFile]

    @model_validator(mode="after")
    def check_file_ids(self) -> ValidationInput:
        unique([f.file_id for f in self.parsed_files], "parsed_files.file_id")
        return self


class ValidationErrorItem(ContractModel):
    item_id: str | None
    field: str
    message: str


T = TypeVar("T")


class ValidationResult(ContractModel, Generic[T]):
    valid: StrictBool
    data: T | None
    errors: list[ValidationErrorItem]


class RelationInput(ExtractionOutput):
    pass


class RelationType(str, Enum):
    about = "about"
    support = "support"
    contradict = "contradict"


class Relation(ContractModel):
    id: Text
    source_id: Text
    target_id: Text
    relation_type: RelationType
    evidence_unit_ids: Annotated[list[Text], Field(min_length=1)]
    rationale: Text
    certainty: Literal["explicit", "inferred"]

    @model_validator(mode="after")
    def check_endpoints(self) -> Relation:
        if self.source_id == self.target_id:
            raise ValueError("relation endpoints must differ")
        unique(self.evidence_unit_ids, "evidence_unit_ids")
        return self


class RelationOutput(ContractModel):
    relations: list[Relation]

    @model_validator(mode="after")
    def check_ids(self) -> RelationOutput:
        unique([r.id for r in self.relations], "relations.id")
        keys = [(r.source_id, r.target_id, r.relation_type) for r in self.relations]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate relations")
        return self


class ReasoningInput(ContractModel):
    case_id: Text
    question: Text
    selected_info_unit_ids: list[Text]
    selected_relation_ids: list[Text]


class ReasoningContext(RelationInput):
    question: Text
    relations: list[Relation]
    selected_info_unit_ids: list[Text]
    selected_relation_ids: list[Text]

    @model_validator(mode="after")
    def check_scope(self) -> ReasoningContext:
        check_relations(self.info_units, self.relations)
        if not set(self.selected_info_unit_ids) <= {u.id for u in self.info_units}:
            raise ValueError("unknown selected InfoUnit")
        if not set(self.selected_relation_ids) <= {r.id for r in self.relations}:
            raise ValueError("unknown selected Relation")
        return self


class ViewType(str, Enum):
    timeline = "timeline"
    conflict = "conflict"
    hypothesis = "hypothesis"


class ViewSelection(ContractModel):
    view_type: ViewType
    reason: Text


class ComposerInput(ReasoningContext):
    view_type: ViewType


class LayoutItem(ContractModel):
    info_unit_id: Text
    role: Literal["claim", "evidence", "event", "context"]
    emphasis: StrictBool
    default_expanded: StrictBool


class ConflictSection(ContractModel):
    id: Text
    title: Text
    placement: Literal["left", "right", "center"]
    items: list[LayoutItem]


class ConflictView(ContractModel):
    view_type: Literal["conflict"]
    title: Text
    sections: list[ConflictSection]
    missing_information: list[Text]

    @model_validator(mode="after")
    def check_ids(self) -> ConflictView:
        unique([s.id for s in self.sections], "sections.id")
        return self


class TimelineEvent(ContractModel):
    id: Text
    title: Text
    time_label: Text
    sort_date: Text | None
    time_precision: Literal["day", "month", "year", "unknown"]
    summary: Text
    related_info_unit_ids: Annotated[list[Text], Field(min_length=1)]


class TimelineView(ContractModel):
    view_type: Literal["timeline"]
    title: Text
    events: list[TimelineEvent]

    @model_validator(mode="after")
    def check_ids(self) -> TimelineView:
        unique([e.id for e in self.events], "events.id")
        return self


class HypothesisStep(ContractModel):
    id: Text
    claim: Text
    supporting_info_unit_ids: list[Text]
    opposing_info_unit_ids: list[Text]
    depends_on_step_ids: list[Text]


class HypothesisView(ContractModel):
    view_type: Literal["hypothesis"]
    title: Text
    hypothesis: Text
    steps: list[HypothesisStep]
    missing_information: list[Text]

    @model_validator(mode="after")
    def check_dependencies(self) -> HypothesisView:
        unique([s.id for s in self.steps], "steps.id")
        deps = {s.id: set(s.depends_on_step_ids) for s in self.steps}
        if any(not parents <= deps.keys() for parents in deps.values()):
            raise ValueError("unknown hypothesis dependency")
        # Kahn traversal avoids recursion limits on malformed long input.
        remaining = set(deps)
        while remaining:
            ready = {sid for sid in remaining if not deps[sid] & remaining}
            if not ready:
                raise ValueError("cyclic hypothesis dependencies")
            remaining -= ready
        return self


ViewPayload = Annotated[ConflictView | TimelineView | HypothesisView, Field(discriminator="view_type")]


class ReasoningView(ContractModel):
    id: Text
    question: Text
    view_type: ViewType
    selection_reason: Text
    payload: ViewPayload
    created_at: Text

    @model_validator(mode="after")
    def check_view_type(self) -> ReasoningView:
        if self.view_type.value != self.payload.view_type:
            raise ValueError("view_type does not match payload")
        return self


class Point(ContractModel):
    x: float
    y: float


class Viewport(ContractModel):
    zoom: Annotated[float, Field(gt=0)]
    offset_x: float
    offset_y: float


class WorkspaceState(ContractModel):
    selected_node_ids: list[Text]
    focused_node_id: Text | None
    current_view_id: Text | None
    node_positions: dict[str, Point]
    display_levels: dict[str, Annotated[StrictInt, Field(ge=1, le=3)]]
    viewport: Viewport


class LawEntry(ContractModel):
    id: Text
    title: Text
    article_no: Text
    text: Text
    source_url: Text
    effective_status: Literal["effective", "unknown"]
    verified_at: Text | None


class LawRetrieveInput(ContractModel):
    case_id: Text
    issue: Text
    selected_info_unit_ids: list[Text]
    max_results: Annotated[StrictInt, Field(gt=0)]


class LawMatch(ContractModel):
    law_id: Text
    issue: Text
    related_info_unit_ids: list[Text]
    relevance_reason: Text


class LawRetrieveOutput(ContractModel):
    matches: list[LawMatch]


class PipelineStage(str, Enum):
    parse = "parse"
    extraction = "extraction"
    relation = "relation"
    view_selection = "view_selection"
    view_composition = "view_composition"
    law_retrieval = "law_retrieval"


class RunStatus(str, Enum):
    success = "success"
    fallback = "fallback"
    failed = "failed"


class StageRun(ContractModel):
    id: Text
    stage: PipelineStage
    status: RunStatus
    model: Text | None
    prompt_version: Text | None
    input_snapshot_path: Text | None
    output_snapshot_path: Text | None
    error_message: Text | None
    created_at: Text


class EvalCase(ContractModel):
    id: Text
    stage: PipelineStage | Literal["end_to_end"]
    input: dict[str, object]
    expected: dict[str, object]
    checks: list[Text]


def check_relations(units: list[InfoUnit], relations: list[Relation]) -> None:
    lookup = {u.id: u for u in units}
    RelationOutput(relations=relations)
    for r in relations:
        if any(uid not in lookup for uid in [r.source_id, r.target_id, *r.evidence_unit_ids]):
            raise ValueError(f"{r.id}: unknown InfoUnit reference")
        a, b = lookup[r.source_id], lookup[r.target_id]
        valid = {
            RelationType.about: True,
            RelationType.support: b.type != InfoUnitType.entity,
            RelationType.contradict: a.type != InfoUnitType.entity and b.type != InfoUnitType.entity,
        }
        if not valid[r.relation_type]:
            raise ValueError(f"{r.id}: illegal relation endpoint types")


class CaseData(ContractModel):
    case_id: Text
    case_title: Text
    files: list[CaseFile]
    core_issue: Text | None
    issue_set: list[Text]
    info_units: list[InfoUnit]
    relations: list[Relation]
    reasoning_views: list[ReasoningView]
    law_matches: list[LawMatch]
    workspace_state: WorkspaceState
    stage_runs: list[StageRun]
    created_at: Text
    updated_at: Text

    @model_validator(mode="after")
    def check_references(self) -> CaseData:
        ExtractionOutput(core_issue=self.core_issue, issue_set=self.issue_set, info_units=self.info_units)
        for name in ("files", "reasoning_views", "stage_runs"):
            key = "file_id" if name == "files" else "id"
            unique([getattr(x, key) for x in getattr(self, name)], name)
        files = {f.file_id: f for f in self.files}
        for u in self.info_units:
            for ref in u.source_refs:
                if ref.file_id not in files or files[ref.file_id].parse_status != ParseStatus.parsed:
                    raise ValueError(f"{u.id}: source must refer to a parsed case file")
                if ref.source_kind != files[ref.file_id].source_kind:
                    raise ValueError(f"{u.id}: source kind differs from uploaded file metadata")
        check_relations(self.info_units, self.relations)
        unit_ids = {u.id for u in self.info_units}
        for view in self.reasoning_views:
            payload = view.payload
            refs: list[str] = []
            if isinstance(payload, ConflictView):
                refs = [i.info_unit_id for s in payload.sections for i in s.items]
            elif isinstance(payload, TimelineView):
                refs = [uid for e in payload.events for uid in e.related_info_unit_ids]
            elif isinstance(payload, HypothesisView):
                refs = [uid for s in payload.steps for uid in
                        [*s.supporting_info_unit_ids, *s.opposing_info_unit_ids]]
            if not set(refs) <= unit_ids:
                raise ValueError(f"{view.id}: unknown view InfoUnit reference")
        if any(not set(m.related_info_unit_ids) <= unit_ids for m in self.law_matches):
            raise ValueError("unknown LawMatch InfoUnit reference")
        views = {v.id for v in self.reasoning_views}
        if unit_ids & views:
            raise ValueError("InfoUnit and ReasoningView IDs must differ")
        state = self.workspace_state
        if state.current_view_id is not None and state.current_view_id not in views:
            raise ValueError("unknown current_view_id")
        node_ids = unit_ids | views
        workspace_refs = set(state.selected_node_ids) | state.node_positions.keys() | state.display_levels.keys()
        if state.focused_node_id is not None:
            workspace_refs.add(state.focused_node_id)
        if not workspace_refs <= node_ids:
            raise ValueError("unknown workspace node reference")
        return self
