"""LLM adapter using an OpenAI-style payload against GitHub Models."""

import json
import os
from typing import Any

import httpx

from app.evidence_tools import build_evidence_context
from app.mock_data import get_mock_reasoning
from app.schemas import EvidenceInput, EvidenceItem, ReasonResponse


SYSTEM_PROMPT = """You are a legal reasoning engine. Return JSON only.
The JSON must contain:
- evidence_items
- entities
- relations
- events
- claims
- conflicts
- evidence_paths
- recommended_view
- summary

evidence_items fields:
id, type, original_content, source_file, page_or_paragraph, time, producer_or_speaker, is_original_evidence, notes

entities fields:
id, name, type, aliases, source_evidence_ids

relations fields:
id, subject_entity, object_entity, relation_type, time, evidence_sources, confidence_status

events fields:
id, event_type, participant_entities, time, location, description, source_evidence_ids

claims fields:
id, content, source, target_ids, stance, credibility_status, quote

Entity type must be one of: person, location, organization, object, account, time.
stance must be one of: support, oppose, neutral.
confidence_status and credibility_status must be one of: high, medium, low, unknown.
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


def _format_evidence_context(evidences: list[EvidenceInput]) -> tuple[list, str]:
    """Parse uploaded evidences and serialize them into the prompt."""

    parsed_evidences = build_evidence_context(evidences)
    if not parsed_evidences:
        return [], "No uploaded evidences."

    serialized = "\n\n".join(
        [
            f"[{item.type}] {item.name}\n"
            f"tool={item.parser_tool}\n"
            f"content=\n{item.normalized_text}"
            for item in parsed_evidences
        ]
    )
    return parsed_evidences, serialized


def _build_evidence_items(parsed_evidences: list) -> list[EvidenceItem]:
    """Convert parsed evidences into structured evidence items for the LLM and fallback output."""

    evidence_items: list[EvidenceItem] = []
    for index, item in enumerate(parsed_evidences, start=1):
        metadata = item.metadata or {}
        evidence_items.append(
            EvidenceItem(
                id=f"evidence-item-{index}",
                type=item.type,
                original_content=item.normalized_text,
                source_file=metadata.get("file_name") or item.name,
                page_or_paragraph=metadata.get("page_or_paragraph", ""),
                time=metadata.get("time", ""),
                producer_or_speaker=metadata.get("producer_or_speaker", ""),
                is_original_evidence=True,
                notes=metadata.get("parser_detail") or metadata.get("notes") or "",
            )
        )
    return evidence_items


async def run_reasoning(case_text: str, question: str, evidences: list[EvidenceInput] | None = None) -> ReasonResponse:
    """Call GitHub Models when configured; fallback to mock data on any failure."""

    _load_local_env()
    evidences = evidences or []
    parsed_evidences, evidence_context = _format_evidence_context(evidences)
    evidence_items = _build_evidence_items(parsed_evidences)

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
        mock_result = get_mock_reasoning()
        mock_result.parsed_evidences = parsed_evidences
        mock_result.evidence_items = evidence_items or mock_result.evidence_items
        return mock_result

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Case materials:\n{case_text}\n\n"
                    f"Structured evidence items:\n{json.dumps([item.model_dump() for item in evidence_items], ensure_ascii=False, indent=2)}\n\n"
                    f"Uploaded evidences parsed by tools:\n{evidence_context}\n\n"
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
        response_model = ReasonResponse.model_validate(parsed)
        response_model.parsed_evidences = parsed_evidences
        if not response_model.evidence_items:
            response_model.evidence_items = evidence_items
        return response_model
    except Exception:
        mock_result = get_mock_reasoning()
        mock_result.parsed_evidences = parsed_evidences
        mock_result.evidence_items = evidence_items or mock_result.evidence_items
        return mock_result
