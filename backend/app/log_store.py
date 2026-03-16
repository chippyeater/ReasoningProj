"""Persist API responses and LLM logs to local JSON files."""

import json
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from app.schemas import ReasonResponse


LOG_DIR = Path(__file__).resolve().parent.parent / "logs"


def save_reason_response(response: ReasonResponse) -> Path:
    """Write one full response payload to backend/logs as JSON."""

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    file_path = LOG_DIR / f"reason-response-{timestamp}-{uuid4().hex[:8]}.json"
    file_path.write_text(
        json.dumps(response.model_dump(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return file_path
