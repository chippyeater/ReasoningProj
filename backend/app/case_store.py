"""Local storage for case snapshots and per-case artifacts."""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.schemas import CaseData, CaseListItem


DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CURRENT_POINTER_PATH = DATA_DIR / "current_case_id.txt"
CASE_JSON_NAME = "case.json"
UPLOAD_FILES_DIRNAME = "upload_files"
LLM_LOG_DIRNAME = "llm_logs"
INTERACTION_LOG_FILE = "interaction_log.jsonl"


def case_dir(case_id: str) -> Path:
    return DATA_DIR / case_id


def case_json_path(case_id: str) -> Path:
    return case_dir(case_id) / CASE_JSON_NAME


def case_upload_dir(case_id: str) -> Path:
    return case_dir(case_id) / UPLOAD_FILES_DIRNAME


def case_llm_log_dir(case_id: str) -> Path:
    return case_dir(case_id) / LLM_LOG_DIRNAME


def case_interaction_log_path(case_id: str) -> Path:
    return case_dir(case_id) / INTERACTION_LOG_FILE


def append_case_log(case_id: str, log_type: str, payload: dict[str, Any]) -> None:
    if log_type not in {"llm", "interaction"}:
        raise ValueError("log_type must be 'llm' or 'interaction'")
    _ensure_case_dirs(case_id)
    if log_type == "llm":
        target = case_llm_log_dir(case_id) / _build_llm_log_filename(payload)
        normalized = _normalize_llm_log_payload(payload)
        target.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
        return
    target = case_interaction_log_path(case_id)
    with target.open("a", encoding="utf-8") as log_file:
        log_file.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _build_llm_log_filename(payload: dict[str, Any]) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    stage = _slugify_filename_part(payload.get("stage") or "llm")
    status = _slugify_filename_part(payload.get("status") or "unknown")
    return f"{timestamp}_{stage}_{status}_{uuid4().hex[:6]}.json"


def _slugify_filename_part(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9_-]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text or "unknown"


def _normalize_llm_log_payload(payload: dict[str, Any]) -> dict[str, Any]:
    request_snapshot = payload.get("request_snapshot")
    request_meta = payload.get("request_meta")
    raw_reply = payload.get("raw_reply")
    parsed_snapshot = payload.get("parsed_snapshot")

    normalized: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "stage": payload.get("stage"),
        "mode": payload.get("mode"),
        "status": payload.get("status"),
        "message": payload.get("message"),
    }

    if request_snapshot not in (None, {}, []):
        normalized["request"] = request_snapshot

    request_debug = {
        key: value
        for key, value in {
            "sent_sections": payload.get("sent_sections"),
            "request_meta": request_meta,
        }.items()
        if value not in (None, "", [], {})
    }
    if request_debug:
        normalized["request_debug"] = request_debug

    llm_response = {
        key: value
        for key, value in {
            "raw_reply": raw_reply,
            "parsed": parsed_snapshot,
        }.items()
        if value not in (None, "", [], {})
    }
    if llm_response:
        normalized["llm_response"] = llm_response

    reserved = {
        "stage",
        "mode",
        "status",
        "message",
        "request_snapshot",
        "sent_sections",
        "request_meta",
        "raw_reply",
        "parsed_snapshot",
    }
    debug = {
        key: value
        for key, value in payload.items()
        if key not in reserved and value not in (None, "", [], {})
    }
    if debug:
        normalized["debug"] = debug

    return normalized


def _ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _ensure_case_dirs(case_id: str) -> None:
    root = case_dir(case_id)
    root.mkdir(parents=True, exist_ok=True)
    case_upload_dir(case_id).mkdir(parents=True, exist_ok=True)
    case_llm_log_dir(case_id).mkdir(parents=True, exist_ok=True)


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

    for root in sorted(path for path in DATA_DIR.iterdir() if path.is_dir()):
        json_path = root / CASE_JSON_NAME
        if not json_path.exists():
            continue
        try:
            case = CaseData.model_validate_json(json_path.read_text(encoding="utf-8"))
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

    path = case_json_path(case_id)
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

    _ensure_case_dirs(case.case_id)
    path = case_json_path(case.case_id)
    path.write_text(json.dumps(case.model_dump(mode="json"), ensure_ascii=False, indent=2), encoding="utf-8")
    if set_current:
        _write_current_case_id(case.case_id)
    return path


def set_current_case(case_id: str) -> bool:
    """Set active case id if target exists."""

    if not case_json_path(case_id).exists():
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

    root = case_dir(case_id)
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    current_id = _read_current_case_id()
    if current_id == case_id:
        CURRENT_POINTER_PATH.unlink(missing_ok=True)
        remaining = list_cases()
        if remaining:
            _write_current_case_id(remaining[0].case_id)
