"""LLM adapter using an OpenAI-style payload against GitHub Models."""

import json
import os
from typing import Any

import httpx

from app.mock_data import get_mock_reasoning
from app.schemas import ReasonResponse


SYSTEM_PROMPT = """You are a legal reasoning engine. Return JSON only.
The JSON must contain: entities, events, claims, conflicts, evidence_paths, recommended_view, summary.
recommended_view must be one of: conflict_compare, timeline_reasoning, hypothesis_board."""


def _load_local_env() -> None:
    """Load a simple .env file from project root without extra dependencies."""

    if os.getenv("_REASONING_ENV_LOADED") == "1":
        return

    env_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))
    if not os.path.exists(env_path):
        os.environ["_REASONING_ENV_LOADED"] = "1"
        return

    with open(env_path, "r", encoding="utf-8") as env_file:
        for raw_line in env_file:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip("'\"")
            os.environ.setdefault(key, value)

    os.environ["_REASONING_ENV_LOADED"] = "1"


def _extract_json(text: str) -> dict[str, Any]:
    """Parse plain JSON or JSON wrapped in markdown code fences."""

    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.replace("json", "", 1).strip()
    return json.loads(text)


async def run_reasoning(case_text: str, question: str) -> ReasonResponse:
    """Call GitHub Models when configured; fallback to mock data on any failure."""

    _load_local_env()

    api_key = (
        os.getenv("GITHUB_TOKEN", "").strip()
        or os.getenv("GITHUB_MODELS_TOKEN", "").strip()
        or os.getenv("OPENAI_API_KEY", "").strip()
    )
    base_url = (
        os.getenv("GITHUB_ENDPOINT", "").strip()
        or os.getenv("OPENAI_BASE_URL", "").strip()
        or "https://models.github.ai/inference"
    ).rstrip("/")
    model = (
        os.getenv("GITHUB_MODEL_ID", "").strip()
        or os.getenv("OPENAI_MODEL", "").strip()
        or "openai/gpt-4.1-mini"
    )
    api_version = os.getenv("GITHUB_API_VERSION", "2022-11-28")

    if not api_key:
        return get_mock_reasoning()

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Case materials:\n{case_text}\n\n"
                    f"Reasoning question:\n{question}\n\n"
                    "Return JSON only."
                ),
            },
        ],
        "temperature": 0.1,
    }

    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "X-GitHub-Api-Version": api_version,
    }

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
