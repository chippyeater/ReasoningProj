"""Deterministic Quote Unit indexes over the authoritative cached UTF-8 text."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import hashlib
import json
import re
from typing import Literal

from pydantic import Field, field_validator, model_validator

from app import case_store
from app.errors import AppError
from app.schemas import CaseData, CaseFile, ContractModel, Index, ParseStatus, SourceKind, SourceRef, Text


# v2 adds context metadata, complete-index comparison, and bounded fallback splitting.
SEGMENTATION_VERSION = "quote-units-v2"

# Ordinary evidence units stay compact enough to scan in a model request. Records
# get a little more room so fields belonging to one transaction usually stay intact.
MAX_QUOTE_CHARS = 800
MAX_RECORD_CHARS = 1200
MIN_PREFERRED_CHARS = 240
HARD_BOUNDARY_LOOKBACK = 96

QuoteRole = Literal["text", "temporal_context", "message", "record", "group_boundary"]
_SENTENCE_END = re.compile(r"[。！？!?；;]+[”’」』]?\s*")
_STRONG_BOUNDARY = re.compile(r"[。！？!?；;\n]+[”’」』]?\s*")
_CLAUSE_BOUNDARY = re.compile(r"[，,、]\s*")
_SOFT_BOUNDARY = re.compile(r"(?:[ \t]+|[|｜—–_=]{1,})")
_CHAT_BOUNDARY = re.compile(r"^(?:[-—_=*·•]\s*){3,}$")
_MESSAGE_START = re.compile(
    r"^(?:(?:\[[^\]\n]{1,40}\]|【[^】\n]{1,40}】)\s*)?(?P<speaker>[^：:\n]{1,40})[：:](?P<body>.*)$")
_INLINE_TIMESTAMPED_MESSAGE = re.compile(
    r"^(?P<timestamp>(?:(?:\d{4}\s*[-/.]\s*\d{1,2}\s*[-/.]\s*\d{1,2})\s+)?"
    r"(?:上午|下午|中午|凌晨|晚上)?\s*\d{1,2}\s*[:：]\s*\d{2}(?:\s*[:：]\s*\d{2})?)"
    r"\s+(?P<speaker>[^：:\n]{1,40})[：:](?P<body>.*)$")
_TIME_ONLY = re.compile(r"^(?:上午|下午|中午|凌晨|晚上)?\s*(\d{1,2})\s*[:：]\s*(\d{2})(?:\s*[:：]\s*(\d{2}))?$")
_WEEKDAY = r"(?:星期|周)[一二三四五六日天]"
_FULL_DATE_PATTERNS = (
    re.compile(rf"^(\d{{4}})\s*年\s*(\d{{1,2}})\s*月\s*(\d{{1,2}})\s*[日号](?:\s*{_WEEKDAY})?(?:\s+(.+))?$"),
    re.compile(rf"^(\d{{4}})\s*[-/.]\s*(\d{{1,2}})\s*[-/.]\s*(\d{{1,2}})(?:\s*{_WEEKDAY})?(?:[ T]+(.+))?$"),
)
_MONTH_DAY = re.compile(rf"^(\d{{1,2}})\s*月\s*(\d{{1,2}})\s*[日号](?:\s*{_WEEKDAY})?(?:\s+(.+))?$")


class QuoteUnit(ContractModel):
    quote_id: Text
    file_id: Text
    start: Index
    end: Index
    text: Text
    role: QuoteRole = "text"
    group_id: Text | None = None
    message_id: Text | None = None
    context_quote_ids: list[Text] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_range_and_metadata(self) -> QuoteUnit:
        if self.end <= self.start or self.end - self.start != len(self.text):
            raise ValueError("quote unit range must match the Unicode length of text")
        if len(self.context_quote_ids) != len(set(self.context_quote_ids)):
            raise ValueError("context_quote_ids must be unique")
        if self.role == "message":
            if self.group_id is None or self.message_id is None:
                raise ValueError("message Quote Units require group_id and message_id")
        elif self.message_id is not None:
            raise ValueError("only message Quote Units may have message_id")
        if self.role in {"temporal_context", "record"} and self.group_id is None:
            raise ValueError(f"{self.role} Quote Units require group_id")
        if self.role == "text" and (self.group_id is not None or self.context_quote_ids):
            raise ValueError("unclassified text must not claim inferred context")
        return self


class QuoteUnitIndex(ContractModel):
    segmentation_version: Literal[SEGMENTATION_VERSION]
    file_id: Text
    text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    units: list[QuoteUnit]

    @field_validator("units")
    @classmethod
    def check_units(cls, units: list[QuoteUnit]) -> list[QuoteUnit]:
        ids = [unit.quote_id for unit in units]
        if len(ids) != len(set(ids)):
            raise ValueError("quote_id must be unique within one index")
        if any(left.start >= right.start or left.end > right.start for left, right in zip(units, units[1:])):
            raise ValueError("quote units must be strictly ordered and non-overlapping")
        return units

    @model_validator(mode="after")
    def check_file_and_context_ids(self) -> QuoteUnitIndex:
        if any(unit.file_id != self.file_id for unit in self.units):
            raise ValueError("quote unit file_id differs from its index")
        lookup = {unit.quote_id: unit for unit in self.units}
        message_metadata: dict[str, tuple[str, tuple[str, ...]]] = {}
        for unit in self.units:
            for context_id in unit.context_quote_ids:
                context = lookup.get(context_id)
                if context is None:
                    raise ValueError("context_quote_ids must reference an existing Quote Unit")
                if context.role != "temporal_context" or context.end > unit.start:
                    raise ValueError("context references must point to an earlier temporal Quote Unit")
                if context.group_id != unit.group_id:
                    raise ValueError("context and referenced unit must belong to the same group")
            if unit.message_id is not None:
                metadata = (unit.group_id or "", tuple(unit.context_quote_ids))
                previous = message_metadata.setdefault(unit.message_id, metadata)
                if previous != metadata:
                    raise ValueError("all pieces of one message must keep the same context")
        return self


class QuoteIndexEvent(ContractModel):
    file_id: Text
    path: Text
    action: Literal["reused", "rebuilt"]
    reason: Literal["current", "missing", "invalid", "text_hash_changed", "segmentation_version_changed"]
    text_sha256: str
    quote_unit_count: Index


@dataclass
class _Segment:
    start: int
    end: int
    role: QuoteRole = "text"
    group_anchor: int | None = None
    message_anchor: int | None = None
    context_anchors: tuple[int, ...] = ()


def quote_index_relative_path(file_id: str) -> str:
    return f"quote_units/{file_id}.json"


def _trimmed_span(text: str, start: int, end: int) -> tuple[int, int] | None:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return (start, end) if start < end else None


def _line_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for match in re.finditer(r"[^\n]+", text):
        span = _trimmed_span(text, match.start(), match.end())
        if span is not None:
            spans.append(span)
    return spans


def _paragraph_spans(text: str) -> list[tuple[int, int]]:
    """Group hard-wrapped lines until an actual blank-line paragraph boundary."""
    lines = _line_spans(text)
    if not lines:
        return []
    paragraphs: list[tuple[int, int]] = []
    start, end = lines[0]
    for line_start, line_end in lines[1:]:
        if text[end:line_start].count("\n") >= 2:
            paragraphs.append((start, end))
            start = line_start
        end = line_end
    paragraphs.append((start, end))
    return paragraphs


def _sentence_spans(text: str, start: int, end: int) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    cursor = start
    for match in _SENTENCE_END.finditer(text, start, end):
        span = _trimmed_span(text, cursor, match.end())
        if span is not None:
            spans.append(span)
        cursor = match.end()
    tail = _trimmed_span(text, cursor, end)
    if tail is not None:
        spans.append(tail)
    return spans


def _latest_boundary(pattern: re.Pattern[str], text: str, lower: int, upper: int) -> int | None:
    matches = [match.end() for match in pattern.finditer(text, lower, upper) if match.end() < upper]
    return matches[-1] if matches else None


def _looks_token_internal(left: str, right: str) -> bool:
    token = r"[\w年月日号元角分%％￥¥./:-]"
    return bool(re.match(token, left, re.UNICODE) and re.match(token, right, re.UNICODE))


def _fallback_boundary(text: str, cursor: int, upper: int) -> int:
    floor = max(cursor + 1, upper - HARD_BOUNDARY_LOOKBACK)
    for boundary in range(upper, floor - 1, -1):
        if boundary >= len(text) or not _looks_token_internal(text[boundary - 1], text[boundary]):
            return boundary
    return upper


def _split_to_limit(text: str, start: int, end: int, limit: int) -> list[tuple[int, int]]:
    """Split without rewriting: sentence, clause, soft boundary, then bounded cut."""
    result: list[tuple[int, int]] = []
    cursor = start
    while end - cursor > limit:
        upper = cursor + limit
        lower = min(upper, cursor + MIN_PREFERRED_CHARS)
        boundary = None
        for pattern in (_STRONG_BOUNDARY, _CLAUSE_BOUNDARY, _SOFT_BOUNDARY):
            boundary = _latest_boundary(pattern, text, lower, upper + 1)
            if boundary is not None:
                break
        if boundary is None or boundary <= cursor:
            boundary = _fallback_boundary(text, cursor, upper)
        span = _trimmed_span(text, cursor, boundary)
        if span is not None:
            result.append(span)
        cursor = boundary
        while cursor < end and text[cursor].isspace():
            cursor += 1
    tail = _trimmed_span(text, cursor, end)
    if tail is not None:
        result.append(tail)
    return result


def _valid_time(value: str | None) -> bool:
    if value is None:
        return True
    match = _TIME_ONLY.fullmatch(value.strip())
    if match is None:
        return False
    hour, minute, second = (int(part) if part is not None else 0 for part in match.groups())
    return 0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59


def _temporal_kind(value: str) -> Literal["date", "time"] | None:
    candidate = value.strip()
    if candidate.startswith(("时间：", "时间:")):
        candidate = candidate[3:].strip()
    pairs = {"[": "]", "【": "】", "(": ")", "（": "）"}
    if candidate and candidate[0] in pairs and candidate.endswith(pairs[candidate[0]]):
        candidate = candidate[1:-1].strip()
    for pattern in _FULL_DATE_PATTERNS:
        match = pattern.fullmatch(candidate)
        if match is None:
            continue
        year, month, day = (int(match.group(i)) for i in range(1, 4))
        try:
            date(year, month, day)
        except ValueError:
            return None
        return "date" if _valid_time(match.group(4)) else None
    match = _MONTH_DAY.fullmatch(candidate)
    if match is not None:
        month, day = int(match.group(1)), int(match.group(2))
        try:
            date(2000, month, day)
        except ValueError:
            return None
        return "date" if _valid_time(match.group(3)) else None
    return "time" if _valid_time(candidate) and _TIME_ONLY.fullmatch(candidate) else None


def _is_message_start(value: str) -> bool:
    candidate = value.strip()
    match = _INLINE_TIMESTAMPED_MESSAGE.fullmatch(candidate)
    if match is not None and _temporal_kind(match.group("timestamp")) is None:
        return False
    if match is None:
        match = _MESSAGE_START.fullmatch(candidate)
    if match is None:
        return False
    speaker = match.group("speaker").strip()
    if (not speaker or speaker.isdigit() or speaker.lower() in {"http", "https"}
            or _temporal_kind(speaker) is not None):
        return False
    return speaker not in {"时间", "日期"}


def _chat_segments(text: str) -> list[_Segment]:
    segments: list[_Segment] = []
    offset = 0
    group_anchor: int | None = None
    date_anchor: int | None = None
    time_anchor: int | None = None
    active_message: _Segment | None = None
    blank_run = 0
    for raw_line in text.splitlines(keepends=True):
        content = raw_line.rstrip("\r\n")
        span = _trimmed_span(text, offset, offset + len(content))
        offset += len(raw_line)
        if span is None:
            blank_run += 1
            active_message = None
            if blank_run >= 2:
                group_anchor = date_anchor = time_anchor = None
            continue
        blank_run = 0
        start, end = span
        value = text[start:end]
        if _CHAT_BOUNDARY.fullmatch(value):
            segments.append(_Segment(start, end, "group_boundary"))
            group_anchor = date_anchor = time_anchor = None
            active_message = None
            continue
        temporal = _temporal_kind(value)
        if temporal == "date":
            group_anchor = start
            date_anchor = start
            time_anchor = None
            active_message = None
            segments.append(_Segment(start, end, "temporal_context", group_anchor))
            continue
        if temporal == "time":
            if group_anchor is None:
                group_anchor = start
            contexts = (date_anchor,) if date_anchor is not None else ()
            segments.append(_Segment(start, end, "temporal_context", group_anchor,
                                     context_anchors=contexts))
            time_anchor = start
            active_message = None
            continue
        if _is_message_start(value):
            if group_anchor is None:
                group_anchor = start
            contexts = tuple(anchor for anchor in (date_anchor, time_anchor) if anchor is not None)
            active_message = _Segment(start, end, "message", group_anchor, start, contexts)
            segments.append(active_message)
            time_anchor = None
            continue
        if active_message is not None:
            active_message.end = end
        else:
            # Without a recognizable message header this remains plain text;
            # no speaker, group, or temporal relationship is guessed.
            segments.append(_Segment(start, end))
    return segments


def _structured_segments(text: str, file: CaseFile) -> list[_Segment]:
    if file.source_kind == SourceKind.communication:
        base = _chat_segments(text)
        limit = MAX_QUOTE_CHARS
    elif file.source_kind == SourceKind.record:
        base = [_Segment(start, end, "record", start)
                for start, end in _paragraph_spans(text)]
        limit = MAX_RECORD_CHARS
    else:
        base = []
        for line_start, line_end in _line_spans(text):
            for start, end in _sentence_spans(text, line_start, line_end):
                base.append(_Segment(start, end))
        limit = MAX_QUOTE_CHARS

    result: list[_Segment] = []
    for segment in base:
        for start, end in _split_to_limit(text, segment.start, segment.end, limit):
            result.append(_Segment(start, end, segment.role, segment.group_anchor,
                                   segment.message_anchor, segment.context_anchors))
    return result


def segment_quote_spans(text: str, file: CaseFile) -> list[tuple[int, int]]:
    """Public span view retained for callers that do not need context metadata."""
    return [(segment.start, segment.end) for segment in _structured_segments(text, file)]


def _quote_id(file_id: str, text_sha256: str, start: int, end: int, text: str) -> str:
    material = f"{SEGMENTATION_VERSION}\0{file_id}\0{text_sha256}\0{start}\0{end}\0{text}".encode("utf-8")
    return "quote-" + hashlib.sha256(material).hexdigest()[:24]


def _metadata_id(kind: str, file_id: str, text_sha256: str, anchor: int) -> str:
    material = f"{SEGMENTATION_VERSION}\0{kind}\0{file_id}\0{text_sha256}\0{anchor}".encode("utf-8")
    return f"{kind}-" + hashlib.sha256(material).hexdigest()[:20]


def _validate_coverage_and_positions(index: QuoteUnitIndex, file: CaseFile, text: str, digest: str) -> bool:
    covered = bytearray(len(text))
    previous_end = 0
    for unit in index.units:
        if (unit.start < previous_end or unit.end > len(text)
                or text[unit.start:unit.end] != unit.text
                or unit.quote_id != _quote_id(file.file_id, digest, unit.start, unit.end, unit.text)):
            return False
        covered[unit.start:unit.end] = b"\x01" * (unit.end - unit.start)
        previous_end = unit.end
    return all(character.isspace() or covered[position]
               for position, character in enumerate(text))


def build_quote_index(file: CaseFile, text: str) -> QuoteUnitIndex:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    segments = _structured_segments(text, file)
    ids_by_anchor = {
        segment.start: _quote_id(file.file_id, digest, segment.start, segment.end,
                                 text[segment.start:segment.end])
        for segment in segments
    }
    units = [
        QuoteUnit(
            quote_id=ids_by_anchor[segment.start], file_id=file.file_id,
            start=segment.start, end=segment.end, text=text[segment.start:segment.end],
            role=segment.role,
            group_id=(_metadata_id("group", file.file_id, digest, segment.group_anchor)
                      if segment.group_anchor is not None else None),
            message_id=(_metadata_id("message", file.file_id, digest, segment.message_anchor)
                        if segment.message_anchor is not None else None),
            context_quote_ids=[ids_by_anchor[anchor] for anchor in segment.context_anchors],
        )
        for segment in segments
    ]
    if not units:
        raise AppError("quote_index_empty", f"{file.file_id}: parsed text produced no Quote Units", 409)
    index = QuoteUnitIndex(segmentation_version=SEGMENTATION_VERSION, file_id=file.file_id,
                           text_sha256=digest, units=units)
    if not _validate_coverage_and_positions(index, file, text, digest):
        raise AppError("quote_index_incomplete", f"{file.file_id}: Quote Unit segmentation omitted source text", 500)
    return index


def _index_matches_text(index: QuoteUnitIndex, file: CaseFile, text: str, digest: str) -> bool:
    if (index.file_id != file.file_id or index.text_sha256 != digest
            or not _validate_coverage_and_positions(index, file, text, digest)):
        return False
    expected = build_quote_index(file, text)
    return index.model_dump(mode="json") == expected.model_dump(mode="json")


def load_or_build_quote_index(case_id: str, file: CaseFile,
                              events: list[QuoteIndexEvent] | None = None) -> QuoteUnitIndex:
    """Reuse a complete current index or deterministically replace stale cache."""
    text = case_store.read_parsed_document(case_id, file).text
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    relative = quote_index_relative_path(file.file_id)
    target = case_store.artifact_path(case_id, relative)
    existing: QuoteUnitIndex | None = None
    reason: Literal["missing", "invalid", "text_hash_changed", "segmentation_version_changed"] = "missing"
    if target.exists():
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("Quote Unit index must be an object")
            if raw.get("segmentation_version") != SEGMENTATION_VERSION:
                reason = "segmentation_version_changed"
            else:
                existing = QuoteUnitIndex.model_validate(raw, strict=True)
        except (OSError, ValueError):
            reason = "invalid"
        if existing is not None:
            if _index_matches_text(existing, file, text, digest):
                if events is not None:
                    events.append(QuoteIndexEvent(
                        file_id=file.file_id, path=relative, action="reused", reason="current",
                        text_sha256=digest, quote_unit_count=len(existing.units)))
                return existing
            reason = "text_hash_changed" if existing.text_sha256 != digest else "invalid"
    rebuilt = build_quote_index(file, text)
    case_store.write_json(target, rebuilt.model_dump(mode="json"))
    if events is not None:
        events.append(QuoteIndexEvent(
            file_id=file.file_id, path=relative, action="rebuilt", reason=reason,
            text_sha256=digest, quote_unit_count=len(rebuilt.units)))
    return rebuilt


def quote_indexes_for_case(case: CaseData, file_ids: list[str] | None = None,
                           events: list[QuoteIndexEvent] | None = None) -> list[QuoteUnitIndex]:
    files = {file.file_id: file for file in case.files}
    selected = ([file.file_id for file in case.files if file.parse_status == ParseStatus.parsed]
                if file_ids is None else file_ids)
    if not selected or len(selected) != len(set(selected)):
        raise AppError("invalid_file_selection", "Select at least one file without duplicate IDs")
    unknown = set(selected) - files.keys()
    if unknown:
        raise AppError("unknown_file", f"Unknown case file IDs: {sorted(unknown)}")
    return [load_or_build_quote_index(case.case_id, files[file_id], events) for file_id in selected]


def build_quote_model_input(case: CaseData, file_ids: list[str] | None = None,
                            events: list[QuoteIndexEvent] | None = None) -> dict:
    files = {file.file_id: file for file in case.files}
    indexes = quote_indexes_for_case(case, file_ids, events)
    return {
        "case_title": case.case_title,
        "files": [
            {
                "file_id": index.file_id,
                "filename": files[index.file_id].filename,
                "quote_units": [
                    {
                        "quote_id": unit.quote_id, "text": unit.text, "role": unit.role,
                        "group_id": unit.group_id, "message_id": unit.message_id,
                        "context_quote_ids": unit.context_quote_ids,
                    }
                    for unit in index.units
                ],
            }
            for index in indexes
        ],
    }


def quote_catalog(case: CaseData, events: list[QuoteIndexEvent] | None = None) -> dict[str, tuple[QuoteUnit, CaseFile]]:
    files = {file.file_id: file for file in case.files}
    catalog: dict[str, tuple[QuoteUnit, CaseFile]] = {}
    for index in quote_indexes_for_case(case, events=events):
        for unit in index.units:
            if unit.quote_id in catalog:
                raise AppError("duplicate_quote_id", f"Duplicate Quote Unit ID: {unit.quote_id}", 409)
            catalog[unit.quote_id] = (unit, files[index.file_id])
    return catalog


def source_ref_from_quote_unit(case_id: str, unit: QuoteUnit, file: CaseFile) -> SourceRef:
    if unit.role == "group_boundary":
        raise AppError("quote_unit_not_evidence", f"{unit.quote_id}: group boundary is not evidence", 422)
    text = case_store.read_parsed_document(case_id, file).text
    if unit.file_id != file.file_id or unit.end > len(text) or text[unit.start:unit.end] != unit.text:
        raise AppError("quote_unit_stale", f"{unit.quote_id}: Quote Unit no longer matches cached TXT", 409)
    return SourceRef(file_id=file.file_id, source_kind=file.source_kind,
                     quote=text[unit.start:unit.end], start=unit.start, end=unit.end)
