"""LLM adapter using an OpenAI-style payload against GitHub Models."""

import json
import os
from typing import Any

import httpx

from app.evidence_tools import build_evidence_context
from app.mock_data import get_mock_reasoning
from app.schemas import EvidenceInput, EvidenceItem, LLMLog, ReasonResponse


SYSTEM_PROMPT = """你是一个法律推理引擎。只返回 JSON，不要返回 Markdown，不要返回额外解释。
请使用中文填写所有自然语言字段，例如 summary、description、content、quote、notes、relation_type、event_type、location。

返回的 JSON 必须包含以下字段：
- evidence_items
- entities
- relations
- events
- claims
- conflicts
- evidence_paths
- recommended_view
- summary

evidence_items 字段：
id, type, original_content, source_file, page_or_paragraph, time, producer_or_speaker, is_original_evidence, notes

entities 字段：
id, name, type, aliases, source_evidence_ids

relations 字段：
id, subject_entity, object_entity, relation_type, time, evidence_sources, confidence_status

events 字段：
id, event_type, participant_entities, time, location, description, source_evidence_ids

claims 字段：
id, content, source, target_ids, stance, credibility_status, quote

type 限制：
- Entity.type 只能是 person, location, organization, object, account, time
- Claim.stance 只能是 support, oppose, neutral
- Relation.confidence_status 和 Claim.credibility_status 只能是 high, medium, low, unknown
- recommended_view 只能是 conflict_compare, timeline_reasoning, hypothesis_board"""


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


def _extract_message_text(content: Any) -> str:
    """Normalize OpenAI-style message content into a plain string."""

    if isinstance(content, str):
        return content

    if isinstance(content, list):
        text_parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text_parts.append(str(item.get("text", "")))
        return "\n".join(part for part in text_parts if part).strip()

    return str(content or "")


def _describe_exception(exc: Exception) -> str:
    """Return a stable, readable error string even when str(exc) is empty."""

    detail = str(exc).strip()
    if detail:
        return f"{type(exc).__name__}: {detail}"
    return type(exc).__name__


def _format_evidence_context(evidences: list[EvidenceInput]) -> tuple[list, str]:
    """Parse uploaded evidences and serialize a concise parser summary."""

    parsed_evidences = build_evidence_context(evidences)
    if not parsed_evidences:
        return [], "无上传证据。"

    serialized = "\n\n".join(
        [
            f"[{item.type}] {item.name}\n"
            f"解析工具: {item.parser_tool}\n"
            f"解析说明: {(item.metadata or {}).get('parser_detail', '')}\n"
            f"解析状态: {(item.metadata or {}).get('parse_status', '')}"
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
    llm_log = LLMLog(provider="github_models", model=model, endpoint=base_url)

    if not api_key:
        fallback_reason = "no_token"
        error_detail = "No API token configured. Set GITHUB_TOKEN/GITHUB_MODELS_TOKEN/OPENAI_API_KEY."
        mock_result = get_mock_reasoning(fallback_reason, error_detail)
        llm_log.fallback_reason = "no_token"
        llm_log.error = error_detail
        mock_result.llm_used = False
        mock_result.fallback_reason = fallback_reason
        mock_result.llm_log = llm_log
        mock_result.parsed_evidences = parsed_evidences
        mock_result.evidence_items = evidence_items or mock_result.evidence_items
        return mock_result

    system_prompt = SYSTEM_PROMPT
    user_prompt = (
        f"以下是案件材料与证据，请基于这些内容进行结构化推理。\n\n"
        f"案件材料：\n{case_text}\n\n"
        f"结构化证据条目（这是主要证据输入，请优先基于它输出）：\n"
        f"{json.dumps([item.model_dump() for item in evidence_items], ensure_ascii=False, indent=2)}\n\n"
        f"证据解析摘要（仅用于帮助你理解证据来源和解析方式，不要与上面的证据内容重复计数）：\n"
        f"{evidence_context}\n\n"
        f"推理问题：\n{question}\n\n"
        "请严格返回 JSON，字段值尽量使用中文，除人名、地名等专有名词外。"
    )
    llm_log.prompt_system = system_prompt
    llm_log.prompt_user = user_prompt

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": user_prompt,
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

    response: httpx.Response | None = None

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(f"{base_url}/chat/completions", headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()

        content = _extract_message_text(data["choices"][0]["message"]["content"])
        llm_log.llm_used = True
        llm_log.raw_response = data
        llm_log.raw_content = content
        llm_log.usage = data.get("usage", {})
        llm_log.limits = {
            "x-ratelimit-limit-requests": response.headers.get("x-ratelimit-limit-requests", ""),
            "x-ratelimit-remaining-requests": response.headers.get("x-ratelimit-remaining-requests", ""),
            "x-ratelimit-reset-requests": response.headers.get("x-ratelimit-reset-requests", ""),
            "x-ratelimit-limit-tokens": response.headers.get("x-ratelimit-limit-tokens", ""),
            "x-ratelimit-remaining-tokens": response.headers.get("x-ratelimit-remaining-tokens", ""),
            "x-ratelimit-reset-tokens": response.headers.get("x-ratelimit-reset-tokens", ""),
        }
        parsed = _extract_json(content)
        response_model = ReasonResponse.model_validate(parsed)
        response_model.llm_used = True
        response_model.fallback_reason = ""
        response_model.llm_log = llm_log
        response_model.parsed_evidences = parsed_evidences
        if not response_model.evidence_items:
            response_model.evidence_items = evidence_items
        return response_model
    except httpx.HTTPStatusError as exc:
        fallback_reason = "request_failed_or_invalid_json"
        error_detail = _describe_exception(exc)
        if exc.response is not None:
            llm_log.raw_response = {
                "status_code": exc.response.status_code,
                "response_text": exc.response.text,
            }
        llm_log.fallback_reason = fallback_reason
        llm_log.error = error_detail
        mock_result = get_mock_reasoning(fallback_reason, error_detail)
        mock_result.llm_used = False
        mock_result.fallback_reason = fallback_reason
        mock_result.llm_log = llm_log
        mock_result.parsed_evidences = parsed_evidences
        mock_result.evidence_items = evidence_items or mock_result.evidence_items
        return mock_result
    except httpx.RequestError as exc:
        fallback_reason = "request_failed_or_invalid_json"
        error_detail = _describe_exception(exc)
        llm_log.fallback_reason = fallback_reason
        llm_log.error = error_detail
        llm_log.raw_response = {
            "request_url": str(exc.request.url) if exc.request is not None else "",
        }
        mock_result = get_mock_reasoning(fallback_reason, error_detail)
        mock_result.llm_used = False
        mock_result.fallback_reason = fallback_reason
        mock_result.llm_log = llm_log
        mock_result.parsed_evidences = parsed_evidences
        mock_result.evidence_items = evidence_items or mock_result.evidence_items
        return mock_result
    except (json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError) as exc:
        fallback_reason = "request_failed_or_invalid_json"
        error_detail = _describe_exception(exc)
        llm_log.fallback_reason = fallback_reason
        llm_log.error = error_detail
        if response is not None:
            llm_log.raw_response = {
                "status_code": response.status_code,
                "response_text": response.text,
            }
        mock_result = get_mock_reasoning(fallback_reason, error_detail)
        mock_result.llm_used = False
        mock_result.fallback_reason = fallback_reason
        mock_result.llm_log = llm_log
        mock_result.parsed_evidences = parsed_evidences
        mock_result.evidence_items = evidence_items or mock_result.evidence_items
        return mock_result
    except Exception as exc:
        fallback_reason = "request_failed_or_invalid_json"
        error_detail = _describe_exception(exc)
        mock_result = get_mock_reasoning(fallback_reason, error_detail)
        llm_log.fallback_reason = fallback_reason
        llm_log.error = error_detail
        mock_result.llm_used = False
        mock_result.fallback_reason = fallback_reason
        mock_result.llm_log = llm_log
        mock_result.parsed_evidences = parsed_evidences
        mock_result.evidence_items = evidence_items or mock_result.evidence_items
        return mock_result
