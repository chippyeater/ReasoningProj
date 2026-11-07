"""Bounded, read-only queries over a caller-supplied current CaseData.

These functions never load another case, persist changes, parse files or call an
LLM. Results are JSON-compatible projections of existing domain objects. See
docs/investigation_tools.md for the response contract and retrieval rules.
"""
from __future__ import annotations

import re
import json
import unicodedata

from app.case_store import artifact_path, read_parsed_document
from app.errors import AppError
from app.schemas import CaseData, InfoUnit, InfoUnitType, ParseStatus, PipelineStage, RelationType, RunStatus


DEFAULT_LIMIT = 5
MAX_LIMIT = 8
MAX_NODE_IDS = 5
SUMMARY_CHARS = 120
MAX_QUERY_CHARS = 200
MAX_EVIDENCE_REFS = 12
MAX_EVIDENCE_CHARS = 8000


def recent_extraction_files(case: CaseData) -> set[str]:
    """Backend-only retrieval priority from the latest committed Extraction."""
    run = next((run for run in reversed(case.stage_runs)
                if run.stage == PipelineStage.extraction and run.status == RunStatus.success), None)
    if run is None:
        return set()
    try:
        snapshot = json.loads(artifact_path(case.case_id, run.input_snapshot_path).read_text(encoding="utf-8"))
        if snapshot.get("operation") == "historical_baseline":
            return set()
        ids = snapshot["selected_file_ids"]
        parsed = {file.file_id for file in case.files if file.parse_status == ParseStatus.parsed}
        if (not isinstance(ids, list) or not ids or any(not isinstance(fid, str) or fid not in parsed for fid in ids)
                or len(ids) != len(set(ids))):
            raise ValueError("Invalid committed file scope")
        return set(ids)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, AppError) as exc:
        raise AppError("extraction_scope_unavailable", "Latest committed Extraction scope cannot be read; retrieval priority cannot be established") from exc


def _result(items: list[dict], *, has_more: bool = False) -> dict:
    return {
        "status": "truncated" if has_more else "ok" if items else "empty",
        "items": items,
        "has_more": has_more,
        "error": None,
    }


def _failure(exc: AppError, *, status: str = "invalid_request") -> dict:
    return {"status": status, "items": [], "has_more": False,
            "error": {"code": exc.code, "message": str(exc),
                      **({"validation_details": exc.details} if exc.details else {})}}


def _check_limit(limit: int) -> None:
    if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
        raise AppError("invalid_limit", f"limit must be an integer from 1 to {MAX_LIMIT}")


def _enum_filter(values: list[str] | None, allowed: set[str], field: str) -> set[str]:
    if values is None:
        return allowed
    if (not isinstance(values, list) or not values
            or any(not isinstance(value, str) or value not in allowed for value in values)):
        raise AppError("invalid_filter", f"{field} must be a nonempty list of supported types")
    if len(set(values)) != len(values):
        raise AppError("invalid_filter", f"{field} must not contain duplicates")
    return set(values)


def _selected_units(case: CaseData, info_unit_ids: list[str]) -> list[InfoUnit]:
    if (not isinstance(info_unit_ids, list) or not 1 <= len(info_unit_ids) <= MAX_NODE_IDS
            or any(not isinstance(uid, str) or not uid.strip() for uid in info_unit_ids)):
        raise AppError("invalid_info_unit_ids",
                       f"info_unit_ids must contain 1 to {MAX_NODE_IDS} nonempty IDs")
    if len(set(info_unit_ids)) != len(info_unit_ids):
        raise AppError("invalid_info_unit_ids", "info_unit_ids must not contain duplicates")
    lookup = {unit.id: unit for unit in case.info_units}
    if any(uid not in lookup for uid in info_unit_ids):
        raise AppError("unknown_info_unit", "Every InfoUnit ID must belong to the current case")
    return [lookup[uid] for uid in info_unit_ids]


def _summary(unit: InfoUnit) -> dict:
    # Preserve attribution/classification even when the content preview is cut.
    return {
        "id": unit.id, "type": unit.type.value,
        "subtype": unit.subtype.value if unit.subtype is not None else None,
        "title": unit.title, "content": unit.content[:SUMMARY_CHARS],
        "content_truncated": len(unit.content) > SUMMARY_CHARS,
        "speaker": unit.speaker,
    }


def _normalized(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def _keyword_score(unit: InfoUnit, terms: list[str]) -> tuple[int, int]:
    """Replaceable lexical ranking: coverage first, then title-weighted hits.

    Terms use OR matching against title/content only. Repetition does not boost
    relevance, and neither provenance nor truth is inferred from the score.
    """
    title, content = _normalized(unit.title), _normalized(unit.content)
    coverage = sum(term in title or term in content for term in terms)
    weighted = sum(3 * (term in title) + (term in content) for term in terms)
    return coverage, weighted


def search_info_units(case: CaseData, query: str, types: list[str] | None = None,
                      limit: int = DEFAULT_LIMIT, *, exclude_info_unit_ids: set[str] | frozenset[str] = frozenset()) -> dict:
    """Search title/content; caller-only exclusions precede ranking and limiting.

    Exclusions are current working-memory IDs, never Canvas visibility. Recent
    Extraction sources prioritize matching nodes, including existing node IDs.
    """
    try:
        _check_limit(limit)
        allowed = _enum_filter(types, {kind.value for kind in InfoUnitType}, "types")
        if not isinstance(query, str) or not 1 <= len(query) <= MAX_QUERY_CHARS:
            raise AppError("invalid_query", f"query must contain 1 to {MAX_QUERY_CHARS} characters")
        terms = list(dict.fromkeys(re.findall(r"\w+", _normalized(query))))
        if not terms:
            raise AppError("invalid_query", "query must contain at least one keyword")
        recent_files = recent_extraction_files(case)
    except AppError as exc:
        return _failure(exc)

    ranked = []
    for unit in case.info_units:
        if unit.id not in exclude_info_unit_ids and unit.type.value in allowed:
            coverage, weighted = _keyword_score(unit, terms)
            if coverage:
                recent = any(ref.file_id in recent_files for ref in unit.source_refs)
                ranked.append((not recent, -coverage, -weighted, unit.id, unit))
    ranked.sort(key=lambda item: item[:4])
    return _result([_summary(item[4]) for item in ranked[:limit]], has_more=len(ranked) > limit)


def get_relations(case: CaseData, info_unit_ids: list[str],
                  relation_types: list[str] | None = None, limit: int = DEFAULT_LIMIT) -> dict:
    """Return persisted incoming/outgoing edges incident to the requested nodes.

    Each item contains a bounded relation projection and summaries of its
    endpoints outside the request. No derived actor/target or multi-hop edges.
    """
    try:
        _check_limit(limit)
        selected = {unit.id for unit in _selected_units(case, info_unit_ids)}
        allowed = _enum_filter(relation_types, {kind.value for kind in RelationType}, "relation_types")
    except AppError as exc:
        return _failure(exc)

    matches = sorted(
        (relation for relation in case.relations
         if relation.relation_type.value in allowed
         and (relation.source_id in selected or relation.target_id in selected)),
        key=lambda relation: relation.id,
    )
    lookup = {unit.id: unit for unit in case.info_units}
    items = []
    for relation in matches[:limit]:
        items.append({
            "relation": {
                "id": relation.id, "source_id": relation.source_id,
                "target_id": relation.target_id, "relation_type": relation.relation_type.value,
                "certainty": relation.certainty,
                "rationale": relation.rationale[:SUMMARY_CHARS],
                "rationale_truncated": len(relation.rationale) > SUMMARY_CHARS,
                "evidence_unit_ids": list(relation.evidence_unit_ids[:MAX_LIMIT]),
                "evidence_ids_truncated": len(relation.evidence_unit_ids) > MAX_LIMIT,
            },
            "adjacent_info_units": [
                _summary(lookup[uid]) for uid in (relation.source_id, relation.target_id)
                if uid not in selected
            ],
        })
    return _result(items, has_more=len(matches) > limit)


def search_evidence_gaps(case: CaseData, query: str | None = None, info_unit_ids: list[str] | None = None,
                         limit: int | None = None, offset: int | None = None, *, reasoning) -> dict:
    """Search a server-bound Issue's existing Gaps; never grant source permissions."""
    from app.issue_workspaces import project_reasoning
    try:
        limit = 3 if limit is None else limit
        _check_limit(limit)
        if limit > 3:
            raise AppError("invalid_limit", "At most three Gaps may be read at once")
        offset = 0 if offset is None else offset
        if type(offset) is not int or offset < 0:
            raise AppError("invalid_tool_arguments", "offset must be a nonnegative integer", details={
                "category": "structure", "errors": [{"loc": ["offset"], "code": "invalid_offset",
                    "message": "offset must be a nonnegative integer", "fix": "Set offset to an integer greater than or equal to zero."}]})
        if query is not None and (not isinstance(query, str) or len(query) > MAX_QUERY_CHARS):
            raise AppError("invalid_query", f"query must contain at most {MAX_QUERY_CHARS} characters")
        terms = list(dict.fromkeys(re.findall(r"\w+", _normalized(query or ""))))
        if query and query.strip() and not terms:
            raise AppError("invalid_query", "query must contain at least one keyword")
        ids = {u.id for u in _selected_units(case, info_unit_ids)} if info_unit_ids is not None and info_unit_ids != [] else set()
    except AppError as exc:
        return _failure(exc)
    hypotheses = {h.id: h for h in reasoning.hypotheses}
    ranks = {}
    for gap in reasoning.gaps:
        text = _normalized(" ".join([gap.content, *(hypotheses[hid].content for hid in gap.hypothesis_ids)]))
        overlap = sum(term in text for term in terms)
        related = bool(ids & set(gap.basis_info_unit_ids + gap.addressing_info_unit_ids)) or any(
            link.hypothesis_id in gap.hypothesis_ids and link.info_unit_id in ids for link in reasoning.links)
        if overlap or related or not terms and not ids:
            ranks[gap.id] = (related, overlap, gap.updated_at)
    matches = sorted((g for g in reasoning.gaps if g.id in ranks), key=lambda g: (*ranks[g.id], g.id), reverse=True)
    chosen = matches[offset:offset + limit]
    if not chosen:
        return _result([])
    view = project_reasoning(reasoning, chosen,
        lambda obj: (sum(term in _normalized(obj.content) for term in terms), obj.updated_at, obj.id),
        uids=ids, content_chars=600, hypothesis_limit=5)
    return _result([view], has_more=len(matches) > offset + limit)


def get_evidence(case: CaseData, info_unit_id: str, offset: int = 0, limit: int = 12) -> dict:
    """Return exact SourceRefs, checked against only their existing cached TXT.

    One InfoUnit, in stored SourceRef order, up to 12 full quotes / 8000 characters.
    Pagination keeps original indices. Cache/source failures reject the whole page.
    """
    try:
        if not isinstance(info_unit_id, str) or not info_unit_id.strip():
            raise AppError("invalid_info_unit_id", "info_unit_id must be one nonempty InfoUnit ID")
        if type(offset) is not int or offset < 0:
            raise AppError("invalid_offset", "offset must be a nonnegative integer")
        if type(limit) is not int or not 1 <= limit <= MAX_EVIDENCE_REFS:
            raise AppError("invalid_limit", "limit must be an integer from 1 to 12")
        unit = _selected_units(case, [info_unit_id])[0]
    except AppError as exc:
        return _failure(exc)

    items: list[dict] = []
    documents: dict[str, str] = {}
    files = {file.file_id: file for file in case.files}
    quote_chars = 0
    try:
        for index in range(offset, min(len(unit.source_refs), offset + limit)):
            ref = unit.source_refs[index]
            if len(ref.quote) > MAX_EVIDENCE_CHARS:
                raise AppError("evidence_budget_exceeded",
                               f"SourceRef {index} exceeds the tool's 8000-character budget")
            if quote_chars + len(ref.quote) > MAX_EVIDENCE_CHARS:
                break
            file = files.get(ref.file_id)
            if file is None:
                raise AppError("source_quote_mismatch", "SourceRef file does not belong to the current case", 409)
            if ref.file_id not in documents:
                documents[ref.file_id] = read_parsed_document(case.case_id, file).text
            text = documents[ref.file_id]
            if (ref.source_kind != file.source_kind or ref.end > len(text)
                    or text[ref.start:ref.end] != ref.quote):
                raise AppError("source_quote_mismatch",
                               "SourceRef does not match the current case's cached TXT and metadata", 409)
            items.append({"info_unit_id": unit.id, "source_ref_index": index,
                          "source_ref": ref.model_dump(mode="json")})
            quote_chars += len(ref.quote)
    except AppError as exc:
        return _failure(exc, status="error")
    next_offset = offset + len(items)
    has_more = next_offset < len(unit.source_refs)
    return {**_result(items, has_more=has_more), "total_count": len(unit.source_refs),
            "next_offset": next_offset if has_more else None}
