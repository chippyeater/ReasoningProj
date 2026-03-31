# pyright: reportGeneralTypeIssues=false, reportAttributeAccessIssue=false, reportUndefinedVariable=false, reportMissingImports=false

"""LEGACY MODULE: not used by the current backend flow.
Kept only for historical reference.
"""

"""Fallback response builder used when LLM is unavailable or returns invalid output."""

from app.schemas import ReasonResponse


def get_mock_reasoning(reason: str, error_detail: str = "") -> ReasonResponse:
    """Return an empty response with explicit fallback diagnostics."""

    readable_reason = reason or "unknown_fallback_reason"
    summary = f"Fallback triggered: {readable_reason}."
    if error_detail:
        summary = f"{summary} Detail: {error_detail}"

    return ReasonResponse(
        llm_used=False,
        fallback_reason=readable_reason,
        recommended_view="conflict_compare",
        summary=summary,
    )
