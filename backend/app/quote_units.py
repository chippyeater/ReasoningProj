"""Deterministic Quote Unit indexes over the authoritative cached UTF-8 text."""
from __future__ import annotations

import hashlib
import re
from typing import Literal

from pydantic import Field, field_validator, model_validator

from app import case_store
from app.errors import AppError
from app.schemas import CaseData, CaseFile, ContractModel, FileType, Index, ParseStatus, SourceKind, SourceRef, Text


SEGMENTATION_VERSION = "quote-units-v1"
_SENTENCE_END = re.compile(r"[。！？!?；;]+[”’」』]?\s*")


class QuoteUnit(ContractModel):
    quote_id: Text
    file_id: Text
    start: Index
    end: Index
    text: Text

    @model_validator(mode="after")
    def check_range(self) -> QuoteUnit:
        if self.end <= self.start or self.end - self.start != len(self.text):
            raise ValueError("quote unit range must match the Unicode length of text")
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
        if any(left.end > right.start for left, right in zip(units, units[1:])):
            raise ValueError("quote units must be ordered and non-overlapping")
        return units

    @model_validator(mode="after")
    def check_file_ids(self) -> QuoteUnitIndex:
        if any(unit.file_id != self.file_id for unit in self.units):
            raise ValueError("quote unit file_id differs from its index")
        return self


class QuoteIndexEvent(ContractModel):
    file_id: Text
    path: Text
    action: Literal["reused", "rebuilt"]
    reason: Literal["current", "missing", "invalid", "text_hash_changed"]
    text_sha256: str
    quote_unit_count: Index


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


def _record_spans(text: str) -> list[tuple[int, int]]:
    """Keep fields for one structured record together, separated by blank lines."""
    lines = _line_spans(text)
    if not lines:
        return []
    groups: list[tuple[int, int]] = []
    group_start, group_end = lines[0]
    for start, end in lines[1:]:
        gap = text[group_end:start]
        if gap.count("\n") >= 2:
            groups.append((group_start, group_end))
            group_start = start
        group_end = end
    groups.append((group_start, group_end))
    result: list[tuple[int, int]] = []
    for start, end in groups:
        # A large multi-record block is safer as its original field lines than
        # as one overly broad citation. No fixed-width splitting is used.
        if end - start > 1200:
            result.extend(span for span in lines if start <= span[0] and span[1] <= end)
        else:
            result.append((start, end))
    return result


def segment_quote_spans(text: str, file: CaseFile) -> list[tuple[int, int]]:
    """Segment by document structure without rewriting or joining characters."""
    if file.source_kind == SourceKind.record:
        return _record_spans(text)
    result: list[tuple[int, int]] = []
    for start, end in _line_spans(text):
        if file.source_kind in {SourceKind.document, SourceKind.testimony}:
            result.extend(_sentence_spans(text, start, end))
        elif file.source_kind == SourceKind.communication:
            # One original message line is the primary chat unit. Only very
            # long messages are divided at their own sentence boundaries.
            pieces = _sentence_spans(text, start, end) if end - start > 800 else [(start, end)]
            result.extend(pieces)
        elif file.file_type in {FileType.markdown, FileType.txt} and end - start > 800:
            result.extend(_sentence_spans(text, start, end))
        else:
            result.append((start, end))
    return result


def _quote_id(file_id: str, text_sha256: str, start: int, end: int, text: str) -> str:
    # The whole-document digest intentionally invalidates every old ID after
    # any cached-text change; no old offset is guessed or silently relocated.
    material = f"{SEGMENTATION_VERSION}\0{file_id}\0{text_sha256}\0{start}\0{end}\0{text}".encode("utf-8")
    return "quote-" + hashlib.sha256(material).hexdigest()[:24]


def build_quote_index(file: CaseFile, text: str) -> QuoteUnitIndex:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    units = [
        QuoteUnit(quote_id=_quote_id(file.file_id, digest, start, end, text[start:end]),
                  file_id=file.file_id, start=start, end=end, text=text[start:end])
        for start, end in segment_quote_spans(text, file)
    ]
    if not units:
        raise AppError("quote_index_empty", f"{file.file_id}: parsed text produced no Quote Units", 409)
    return QuoteUnitIndex(segmentation_version=SEGMENTATION_VERSION, file_id=file.file_id,
                          text_sha256=digest, units=units)


def _index_matches_text(index: QuoteUnitIndex, file: CaseFile, text: str, digest: str) -> bool:
    return (index.file_id == file.file_id and index.text_sha256 == digest
            and all(unit.end <= len(text) and text[unit.start:unit.end] == unit.text
                    and unit.quote_id == _quote_id(file.file_id, digest, unit.start, unit.end, unit.text)
                    for unit in index.units))


def load_or_build_quote_index(case_id: str, file: CaseFile,
                              events: list[QuoteIndexEvent] | None = None) -> QuoteUnitIndex:
    """Reuse a current index or deterministically replace a missing/stale cache."""
    text = case_store.read_parsed_document(case_id, file).text
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    relative = quote_index_relative_path(file.file_id)
    target = case_store.artifact_path(case_id, relative)
    existing: QuoteUnitIndex | None = None
    reason: Literal["missing", "invalid", "text_hash_changed"] = "missing"
    if target.exists():
        try:
            existing = QuoteUnitIndex.model_validate_json(target.read_bytes(), strict=True)
        except (OSError, ValueError):
            reason = "invalid"
        else:
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
                    {"quote_id": unit.quote_id, "text": unit.text}
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
    text = case_store.read_parsed_document(case_id, file).text
    if unit.end > len(text) or text[unit.start:unit.end] != unit.text:
        raise AppError("quote_unit_stale", f"{unit.quote_id}: Quote Unit no longer matches cached TXT", 409)
    return SourceRef(file_id=file.file_id, source_kind=file.source_kind,
                     quote=text[unit.start:unit.end], start=unit.start, end=unit.end)
