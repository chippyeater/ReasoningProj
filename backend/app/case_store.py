"""Local storage for case snapshots and current selection."""

from __future__ import annotations

import json
from pathlib import Path

from app.schemas import CaseData, CaseListItem


DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CASES_DIR = DATA_DIR / "cases"
CURRENT_POINTER_PATH = DATA_DIR / "current_case_id.txt"


def _case_path(case_id: str) -> Path:
    return CASES_DIR / f"{case_id}.json"


def _ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CASES_DIR.mkdir(parents=True, exist_ok=True)


def _read_current_case_id() -> str | None:
    if not CURRENT_POINTER_PATH.exists():
        return None
    value = CURRENT_POINTER_PATH.read_text(encoding="utf-8").strip()
    return value or None


def _write_current_case_id(case_id: str) -> None:
    _ensure_dirs()
    CURRENT_POINTER_PATH.write_text(case_id, encoding="utf-8")


def list_cases() -> list[CaseListItem]:
    """List all saved cases with selector metadata."""

    _ensure_dirs()
    current_id = _read_current_case_id()
    items: list[CaseListItem] = []

    for path in sorted(CASES_DIR.glob("*.json")):
        try:
            case = CaseData.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        items.append(
            CaseListItem(
                case_id=case.case_id,
                case_title=case.case_title or case.case_id,
                updated_at=case.updated_at,
                is_current=case.case_id == current_id,
            )
        )

    return sorted(items, key=lambda item: item.updated_at, reverse=True)


def load_case(case_id: str) -> CaseData | None:
    """Load one case by id."""

    path = _case_path(case_id)
    if not path.exists():
        return None
    return CaseData.model_validate_json(path.read_text(encoding="utf-8"))


def load_current_case() -> CaseData | None:
    """Load selected current case."""

    _ensure_dirs()
    current_id = _read_current_case_id()
    if current_id:
        current_case = load_case(current_id)
        if current_case:
            return current_case

    cases = list_cases()
    if not cases:
        return None
    set_current_case(cases[0].case_id)
    return load_case(cases[0].case_id)


def save_case(case: CaseData, set_current: bool = True) -> Path:
    """Save or update one case."""

    _ensure_dirs()
    path = _case_path(case.case_id)
    path.write_text(json.dumps(case.model_dump(mode="json"), ensure_ascii=False, indent=2), encoding="utf-8")
    if set_current:
        _write_current_case_id(case.case_id)
    return path


def set_current_case(case_id: str) -> bool:
    """Set active case id if target exists."""

    if not _case_path(case_id).exists():
        return False
    _write_current_case_id(case_id)
    return True


def clear_current_case() -> None:
    """Delete selected current case snapshot if present."""

    current_id = _read_current_case_id()
    if not current_id:
        return
    clear_case(current_id)


def clear_case(case_id: str) -> None:
    """Delete one case by id."""

    path = _case_path(case_id)
    path.unlink(missing_ok=True)
    current_id = _read_current_case_id()
    if current_id == case_id:
        CURRENT_POINTER_PATH.unlink(missing_ok=True)
        remaining = list_cases()
        if remaining:
            _write_current_case_id(remaining[0].case_id)
