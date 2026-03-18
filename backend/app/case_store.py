"""Local storage for the single current case."""

import json
from pathlib import Path

from app.schemas import CurrentCase


DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CURRENT_CASE_PATH = DATA_DIR / "current_case.json"


def load_current_case() -> CurrentCase | None:
    """Load the current case from disk if it exists."""

    if not CURRENT_CASE_PATH.exists():
        return None
    return CurrentCase.model_validate_json(CURRENT_CASE_PATH.read_text(encoding="utf-8"))


def save_current_case(case: CurrentCase) -> Path:
    """Persist the current case to disk."""

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CURRENT_CASE_PATH.write_text(
        json.dumps(case.model_dump(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return CURRENT_CASE_PATH


def clear_current_case() -> None:
    """Delete the current case snapshot if present."""

    if CURRENT_CASE_PATH.exists():
        CURRENT_CASE_PATH.unlink()
