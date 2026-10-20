"""One explicit configured provider, one request, strict response handling."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.errors import AppError

ENV_PATH = Path(__file__).resolve().parents[2] / ".env"


def configuration_values() -> dict[str, str]:
    values: dict[str, str] = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip("\"'")
    values.update(os.environ)
    return values


def redact(value: object, values: dict[str, str] | None = None) -> object:
    config = configuration_values() if values is None else values
    secrets = [v for k, v in config.items() if v and (
        "API_KEY" in k.upper() or "SECRET" in k.upper() or k.upper().endswith("_TOKEN"))]
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, dict):
        return {k: redact(v, config) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, config) for v in value]
    return value


@dataclass(frozen=True)
class LLMConfig:
    provider: str
    base_url: str
    model: str
    api_key: str = field(repr=False)
    timeout_seconds: float = 120.0
    max_completion_tokens: int = 20000

    def public_metadata(self) -> dict:
        return {"provider": self.provider, "base_url": self.base_url, "model": self.model,
                "timeout_seconds": self.timeout_seconds, "max_completion_tokens": self.max_completion_tokens}


def load_config() -> LLMConfig:
    values = configuration_values()
    if any(values.get(k, "").strip() for k in ("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_MODEL")):
        provider = "azure"
        key = values.get("AZURE_OPENAI_API_KEY", "").strip()
        base = values.get("AZURE_OPENAI_ENDPOINT", "").strip().rstrip("/")
        model = values.get("AZURE_OPENAI_MODEL", "").strip()
        parsed = urlsplit(base)
        host = parsed.hostname or ""
        if host.endswith((".services.ai.azure.com", ".openai.azure.com")):
            # Project-scoped management URLs are not model inference base URLs.
            if not parsed.path or parsed.path.startswith("/api/projects/"):
                base = urlunsplit((parsed.scheme, parsed.netloc, "/openai/v1", "", ""))
            elif parsed.path != "/openai/v1":
                raise AppError("llm_configuration_invalid", "Azure endpoint must use the v1 model inference route", 503)
    elif any(values.get(k, "").strip() for k in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL")):
        provider = "openai"
        key = values.get("OPENAI_API_KEY", "").strip()
        base = values.get("OPENAI_BASE_URL", "https://api.openai.com/v1").strip().rstrip("/")
        model = values.get("OPENAI_MODEL", "").strip()
    elif any(values.get(k, "").strip() for k in ("GITHUB_MODELS_TOKEN", "GITHUB_TOKEN", "GITHUB_MODEL_ID")):
        provider = "github"
        key = values.get("GITHUB_MODELS_TOKEN", values.get("GITHUB_TOKEN", "")).strip()
        base = values.get("GITHUB_ENDPOINT", "https://models.github.ai/inference").strip().rstrip("/")
        model = values.get("GITHUB_MODEL_ID", "").strip()
    else:
        raise AppError("llm_configuration_missing", "No model provider has been configured", 503)
    endpoint = urlsplit(base)
    if not key or not base or not model:
        raise AppError("llm_configuration_missing", "Configured provider requires an API key, endpoint and explicit model", 503)
    if endpoint.scheme != "https" or not endpoint.hostname or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
        raise AppError("llm_configuration_invalid", "Model endpoint must be an HTTPS URL without credentials or query parameters", 503)
    try:
        timeout = float(values.get("LLM_TIMEOUT_SECONDS", "120"))
        tokens = int(values.get("LLM_MAX_COMPLETION_TOKENS", "20000"))
        if not 0 < timeout <= 600 or tokens <= 0:
            raise ValueError("Invalid request limit")
    except ValueError as exc:
        raise AppError("llm_configuration_invalid", "Invalid model timeout or completion token limit", 503) from exc
    return LLMConfig(provider, base, model, key, timeout, tokens)


@dataclass
class LLMReply:
    content: str
    response: dict


class LLMFailure(AppError):
    def __init__(self, code: str, message: str, *, response: object = None):
        super().__init__(code, message, 502)
        self.response = response


async def call_structured(config: LLMConfig, system_prompt: str, model_input: dict, schema: dict) -> LLMReply:
    payload = {
        "model": config.model,
        "messages": [{"role": "system", "content": system_prompt},
                     {"role": "user", "content": json.dumps(model_input, ensure_ascii=False)}],
        "response_format": {"type": "json_schema", "json_schema": {
            "name": "logicweaver_extraction", "strict": True, "schema": schema}},
        "max_completion_tokens": config.max_completion_tokens,
    }
    headers = {"Content-Type": "application/json"}
    if config.provider == "azure":
        headers["api-key"] = config.api_key
    else:
        headers["Authorization"] = f"Bearer {config.api_key}"
    if config.provider == "github":
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    try:
        async with httpx.AsyncClient(timeout=config.timeout_seconds, follow_redirects=False) as client:
            response = await client.post(f"{config.base_url}/chat/completions", headers=headers, json=payload)
    except httpx.TimeoutException as exc:
        raise LLMFailure("llm_timeout", "Model request timed out", response={"exception_type": type(exc).__name__}) from exc
    except httpx.HTTPError as exc:
        raise LLMFailure("llm_call_failed", "Model request failed at the transport boundary", response={"exception_type": type(exc).__name__}) from exc
    if response.status_code != 200:
        raise LLMFailure("llm_call_failed", f"Model provider returned HTTP {response.status_code}", response=response.text)
    try:
        body = response.json()
        if not isinstance(body, dict) or not isinstance(body.get("choices"), list) or len(body["choices"]) != 1:
            raise ValueError("Expected exactly one completion")
        choice = body["choices"][0]
        if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
            raise ValueError("Invalid completion message")
        message = choice["message"]
        if message.get("refusal"):
            raise LLMFailure("llm_refused", "Model refused the extraction request", response=body)
        if choice.get("finish_reason") != "stop":
            raise LLMFailure("llm_incomplete", "Model response did not finish normally", response=body)
        content = message["content"]
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Missing model content")
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise LLMFailure("llm_response_invalid", "Provider returned an invalid completion envelope", response=response.text) from exc
    return LLMReply(content=content, response=body)
