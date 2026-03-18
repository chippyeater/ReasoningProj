"""Persist API payloads to local JSON files."""

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel


LOG_DIR = Path(__file__).resolve().parent.parent / "logs"


def save_log_payload(prefix: str, payload: BaseModel | dict[str, Any]) -> Path:
    """Write one payload to backend/logs as JSON."""

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    file_path = LOG_DIR / f"{prefix}-{timestamp}-{uuid4().hex[:8]}.json"
    content = payload.model_dump() if isinstance(payload, BaseModel) else payload
    file_path.write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")
    return file_path
