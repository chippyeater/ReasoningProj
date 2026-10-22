"""Bounded, read-only queries over a caller-supplied current CaseData.

These functions never load another case, persist changes, parse files or call an
LLM. Results are JSON-compatible projections of existing domain objects. See
docs/investigation_tools.md for the response contract and retrieval rules.
"""
from __future__ import annotations

import re
import unicodedata

from app.case_store import read_parsed_document
from app.errors import AppError
from app.schemas import CaseData, InfoUnit, InfoUnitType, RelationType


DEFAULT_LIMIT = 5
MAX_LIMIT = 8
MAX_NODE_IDS = 5
SUMMARY_CHARS = 120
MAX_QUERY_CHARS = 200
MAX_EVIDENCE_REFS = 8
MAX_EVIDENCE_CHARS = 4000


def _result(items: list[dict], *, has_more: bool = False) -> dict:
    return {
        "status": "truncated" if has_more else "ok" if items else "empty",
        "items": items,
        "has_more": has_more,
        "error": None,
    }


def _failure(exc: AppError, *, status: str = "invalid_request") -> dict:
    return {"status": status, "items": [], "has_more": False,
            "error": {"code": exc.code, "message": str(exc)}}


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
                      limit: int = DEFAULT_LIMIT) -> dict:
    """Search title/content using literal keywords, returning at most 8 previews."""
    try:
        _check_limit(limit)
        allowed = _enum_filter(types, {kind.value for kind in InfoUnitType}, "types")
        if not isinstance(query, str) or not 1 <= len(query) <= MAX_QUERY_CHARS:
            raise AppError("invalid_query", f"query must contain 1 to {MAX_QUERY_CHARS} characters")
        terms = list(dict.fromkeys(re.findall(r"\w+", _normalized(query))))
        if not terms:
            raise AppError("invalid_query", "query must contain at least one keyword")
    except AppError as exc:
        return _failure(exc)

    ranked = []
    for unit in case.info_units:
        if unit.type.value in allowed:
            coverage, weighted = _keyword_score(unit, terms)
            if coverage:
                ranked.append((-coverage, -weighted, unit.id, unit))
    ranked.sort(key=lambda item: item[:3])
    return _result([_summary(item[3]) for item in ranked[:limit]], has_more=len(ranked) > limit)


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


def get_evidence(case: CaseData, info_unit_ids: list[str]) -> dict:
    """Return exact SourceRefs, checked against only their existing cached TXT.

    At most 5 requested nodes, 8 references and 4000 quote characters. References
    are never shortened. Cache/source failures reject the complete response.
    """
    try:
        units = _selected_units(case, info_unit_ids)
    except AppError as exc:
        return _failure(exc)

    items: list[dict] = []
    documents: dict[str, str] = {}
    files = {file.file_id: file for file in case.files}
    quote_chars = 0
    try:
        for unit in units:
            for index, ref in enumerate(unit.source_refs):
                if len(items) == MAX_EVIDENCE_REFS:
                    return _result(items, has_more=True)
                if len(ref.quote) > MAX_EVIDENCE_CHARS:
                    raise AppError("evidence_budget_exceeded",
                                   "A complete SourceRef exceeds the tool's 4000-character budget")
                if quote_chars + len(ref.quote) > MAX_EVIDENCE_CHARS:
                    return _result(items, has_more=True)
                file = files[ref.file_id]
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
    return _result(items)
