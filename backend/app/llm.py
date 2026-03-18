"""LLM pipeline for case extraction and question reasoning."""

import json
import os
from typing import Any

import httpx

from app.evidence_tools import build_evidence_context
from app.schemas import (
    CaseQuestionResponse,
    CurrentCase,
    EvidenceInput,
    EvidenceItem,
    ExtractionStageResponse,
    PipelineLog,
    QuestionReasoningStageResponse,
    StageLog,
)


def _read_prompt_markdown(filename: str) -> str:
    """Load a prompt from backend/prompts and strip the markdown code fence wrapper."""

    prompt_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "prompts", filename))
    try:
        with open(prompt_path, "r", encoding="utf-8") as prompt_file:
            content = prompt_file.read().strip()
    except OSError:
        raise ValueError(f"Prompt file not found: {filename}")

    if "```text" in content:
        content = content.split("```text", 1)[1]
    if "```" in content:
        content = content.split("```", 1)[0]

    content = content.strip()
    return content


EXTRACTION_SYSTEM_PROMPT = _read_prompt_markdown(
    "extraction_system_prompt.md"
)
QUESTION_REASONING_SYSTEM_PROMPT = _read_prompt_markdown(
    "question_reasoning_system_prompt.md"
)


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
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))

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
    return f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__


def _provider_config() -> tuple[str, str, str, str]:
    """Return provider endpoint configuration."""

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
    return api_key, base_url, model, api_version


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


def build_evidence_items(parsed_evidences: list) -> list[EvidenceItem]:
    """Convert parsed evidences into internal evidence items."""

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


def prepare_case_materials(
    case_text: str, evidences: list[EvidenceInput] | None = None
) -> tuple[list, list[EvidenceItem], str]:
    """Normalize raw evidence into parsed evidence, internal items, and parser summary."""

    parsed_evidences, evidence_context = _format_evidence_context(evidences or [])
    evidence_items = build_evidence_items(parsed_evidences)
    return parsed_evidences, evidence_items, evidence_context


async def _call_stage(
    stage_name: str,
    system_prompt: str,
    user_prompt: str,
) -> tuple[dict[str, Any] | None, StageLog]:
    """Call the provider for one pipeline stage and return raw parsed JSON plus a stage log."""

    api_key, base_url, model, api_version = _provider_config()
    stage_log = StageLog(stage_name=stage_name, prompt_system=system_prompt, prompt_user=user_prompt)

    if not api_key:
        stage_log.fallback_used = True
        stage_log.fallback_reason = "no_token"
        stage_log.error = "No API token configured. Set GITHUB_TOKEN/GITHUB_MODELS_TOKEN/OPENAI_API_KEY."
        return None, stage_log

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
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
        stage_log.llm_used = True
        stage_log.raw_response = data
        stage_log.raw_content = content
        stage_log.usage = data.get("usage", {})
        stage_log.limits = {
            "x-ratelimit-limit-requests": response.headers.get("x-ratelimit-limit-requests", ""),
            "x-ratelimit-remaining-requests": response.headers.get("x-ratelimit-remaining-requests", ""),
            "x-ratelimit-reset-requests": response.headers.get("x-ratelimit-reset-requests", ""),
            "x-ratelimit-limit-tokens": response.headers.get("x-ratelimit-limit-tokens", ""),
            "x-ratelimit-remaining-tokens": response.headers.get("x-ratelimit-remaining-tokens", ""),
            "x-ratelimit-reset-tokens": response.headers.get("x-ratelimit-reset-tokens", ""),
        }
        return _extract_json(content), stage_log
    except httpx.HTTPStatusError as exc:
        stage_log.fallback_used = True
        stage_log.fallback_reason = "request_failed_or_invalid_json"
        stage_log.error = _describe_exception(exc)
        if exc.response is not None:
            stage_log.raw_response = {
                "status_code": exc.response.status_code,
                "response_text": exc.response.text,
            }
    except httpx.RequestError as exc:
        stage_log.fallback_used = True
        stage_log.fallback_reason = "request_failed_or_invalid_json"
        stage_log.error = _describe_exception(exc)
        stage_log.raw_response = {"request_url": str(exc.request.url) if exc.request is not None else ""}
    except (json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError) as exc:
        stage_log.fallback_used = True
        stage_log.fallback_reason = "request_failed_or_invalid_json"
        stage_log.error = _describe_exception(exc)
        if response is not None:
            stage_log.raw_response = {
                "status_code": response.status_code,
                "response_text": response.text,
            }
    except Exception as exc:
        stage_log.fallback_used = True
        stage_log.fallback_reason = "request_failed_or_invalid_json"
        stage_log.error = _describe_exception(exc)

    return None, stage_log


def _pipeline_log_from_stage(stage_log: StageLog) -> PipelineLog:
    """Build a pipeline log from a single stage log."""

    _, base_url, model, _ = _provider_config()
    return PipelineLog(
        provider="github_models",
        model=model,
        endpoint=base_url,
        pipeline_llm_used=stage_log.llm_used,
        fallback_reason=stage_log.fallback_reason,
        stages=[stage_log],
    )


async def run_extraction_stage(
    case_text: str, evidence_items: list[EvidenceItem], evidence_context: str
) -> ExtractionStageResponse:
    """Run stage 1: extract entities, relations, events, and claims."""

    user_prompt = (
        f"案件介绍：\n{case_text or '无'}\n\n"
        f"结构化证据条目（系统内部生成，作为主要输入）：\n"
        f"{json.dumps([item.model_dump() for item in evidence_items], ensure_ascii=False, indent=2)}\n\n"
        f"证据解析摘要：\n{evidence_context}\n\n"
        "请严格输出 entities、relations、events、claims 四个顶层字段，且每个数组元素都必须符合 schema 要求。"
    )
    raw_data, stage_log = await _call_stage("extraction", EXTRACTION_SYSTEM_PROMPT, user_prompt)
    if raw_data is None:
        return ExtractionStageResponse(stage_log=stage_log)

    try:
        result = ExtractionStageResponse.model_validate(raw_data)
        result.stage_log = stage_log
        return result
    except Exception as exc:
        stage_log.fallback_used = True
        stage_log.fallback_reason = "invalid_schema"
        stage_log.error = _describe_exception(exc)
        return ExtractionStageResponse(stage_log=stage_log)


async def run_question_reasoning_stage(current_case: CurrentCase, question: str) -> QuestionReasoningStageResponse:
    """Run stage 2+3: generate reasoning structure and interface recommendation."""

    user_prompt = (
        f"用户问题：\n{question}\n\n"
        f"案件介绍：\n{current_case.case_text or '无'}\n\n"
        f"证据条目：\n{json.dumps([item.model_dump() for item in current_case.evidence_items], ensure_ascii=False, indent=2)}\n\n"
        f"已抽取实体：\n{json.dumps([item.model_dump() for item in current_case.entities], ensure_ascii=False, indent=2)}\n\n"
        f"已抽取关系：\n{json.dumps([item.model_dump() for item in current_case.relations], ensure_ascii=False, indent=2)}\n\n"
        f"已抽取事件：\n{json.dumps([item.model_dump() for item in current_case.events], ensure_ascii=False, indent=2)}\n\n"
        f"已抽取主张：\n{json.dumps([item.model_dump() for item in current_case.claims], ensure_ascii=False, indent=2)}\n\n"
        "请围绕这个问题，严格输出 conflicts、evidence_paths、recommended_view、summary 四个顶层字段。"
    )
    raw_data, stage_log = await _call_stage("question_reasoning", QUESTION_REASONING_SYSTEM_PROMPT, user_prompt)
    if raw_data is None:
        return QuestionReasoningStageResponse(
            question=question,
            recommended_view="conflict_compare",
            summary=f"问题推理阶段回退：{stage_log.fallback_reason or 'unknown'}。",
            stage_log=stage_log,
        )

    try:
        result = QuestionReasoningStageResponse.model_validate(raw_data)
        result.question = question
        result.stage_log = stage_log
        return result
    except Exception as exc:
        stage_log.fallback_used = True
        stage_log.fallback_reason = "invalid_schema"
        stage_log.error = _describe_exception(exc)
        return QuestionReasoningStageResponse(
            question=question,
            recommended_view="conflict_compare",
            summary="问题推理阶段返回格式无效，已回退。",
            stage_log=stage_log,
        )


async def build_current_case(
    case_text: str, evidences: list[EvidenceInput] | None = None, case_id: str = ""
) -> CurrentCase:
    """Create the current case snapshot from raw case text and evidences."""

    parsed_evidences, evidence_items, evidence_context = prepare_case_materials(case_text, evidences)
    extraction = await run_extraction_stage(case_text, evidence_items, evidence_context)
    return CurrentCase(
        case_id=case_id,
        case_text=case_text,
        parsed_evidences=parsed_evidences,
        evidence_items=evidence_items,
        entities=extraction.entities,
        relations=extraction.relations,
        events=extraction.events,
        claims=extraction.claims,
        extraction_log=_pipeline_log_from_stage(extraction.stage_log),
    )


async def answer_case_question(current_case: CurrentCase, question: str) -> CaseQuestionResponse:
    """Answer a question against the current case."""

    reasoning = await run_question_reasoning_stage(current_case, question)
    return CaseQuestionResponse(
        case_id=current_case.case_id,
        question=question,
        conflicts=reasoning.conflicts,
        evidence_paths=reasoning.evidence_paths,
        recommended_view=reasoning.recommended_view,
        summary=reasoning.summary,
        reasoning_log=_pipeline_log_from_stage(reasoning.stage_log),
    )
