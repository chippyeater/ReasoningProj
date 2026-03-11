"""LLM adapter with OpenAI-compatible API and mock fallback strategy."""

import json
import os
from typing import Any

import httpx

from app.mock_data import get_mock_reasoning
from app.schemas import ReasonResponse


SYSTEM_PROMPT = """你是司法推理结构化引擎。请仅返回 JSON，不要返回任何额外文字。
JSON 字段必须包含：entities, events, claims, conflicts, evidence_paths, recommended_view, summary。
recommended_view 必须是 conflict_compare / timeline_reasoning / hypothesis_board 之一。"""


def _extract_json(text: str) -> dict[str, Any]:
    """Parse plain JSON or JSON wrapped in markdown code fences."""

    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.replace("json", "", 1).strip()
    return json.loads(text)


async def run_reasoning(case_text: str, question: str) -> ReasonResponse:
    """Call LLM when configured; fallback to mock data on any failure."""

    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

    if not api_key:
        return get_mock_reasoning()

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"案件材料：\n{case_text}\n\n推理问题：\n{question}\n\n只返回 JSON。",
            },
        ],
        "temperature": 0.1,
    }

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(f"{base_url}/chat/completions", headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()

        content = data["choices"][0]["message"]["content"]
        parsed = _extract_json(content)
        return ReasonResponse.model_validate(parsed)
    except Exception:
        return get_mock_reasoning()
