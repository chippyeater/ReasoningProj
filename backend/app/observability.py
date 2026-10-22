"""Failure-isolated Langfuse tracing for LogicWeaver model workflows."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import logging
import time
from typing import Iterator

from app import llm_client

LOGGER = logging.getLogger(__name__)


def _boolean(values: dict[str, str], key: str, default: bool = False) -> bool:
    raw = values.get(key, "true" if default else "false").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off", ""}:
        return False
    LOGGER.warning("Ignoring invalid %s value; observability remains disabled", key)
    return False


@dataclass(frozen=True)
class ObservabilitySettings:
    enabled: bool
    capture_content: bool
    public_key: str | None
    secret_key: str | None
    base_url: str
    environment: str


def load_settings() -> ObservabilitySettings:
    values = llm_client.configuration_values()
    return ObservabilitySettings(
        enabled=_boolean(values, "LOGICWEAVER_OBSERVABILITY_ENABLED"),
        capture_content=_boolean(values, "LOGICWEAVER_OBSERVABILITY_CAPTURE_CONTENT"),
        public_key=values.get("LANGFUSE_PUBLIC_KEY", "").strip() or None,
        secret_key=values.get("LANGFUSE_SECRET_KEY", "").strip() or None,
        base_url=values.get("LANGFUSE_BASE_URL", "https://cloud.langfuse.com").strip().rstrip("/"),
        environment=values.get("LANGFUSE_TRACING_ENVIRONMENT", "development").strip() or "development",
    )


def build_run_name(case_title: str, created_at: str, stage: str) -> str:
    short_title = " ".join(case_title.split())[:24] or "未命名案件"
    try:
        moment = datetime.fromisoformat(created_at.replace("Z", "+00:00")).astimezone()
        display_time = moment.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        display_time = created_at
    return f"{short_title} · {display_time} · {stage}"


def _json_bytes(value: object) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    except (TypeError, ValueError):
        return repr(value).encode("utf-8", errors="replace")


def _summary(value: object) -> dict[str, object]:
    raw = _json_bytes(value)
    result: dict[str, object] = {
        "content_captured": False,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "utf8_bytes": len(raw),
    }
    if isinstance(value, dict):
        result["top_level_keys"] = sorted(str(key) for key in value)
        for key in ("status", "valid", "action", "error_code", "exception_type", "error_stage",
                    "model_skipped"):
            scalar = value.get(key)
            if isinstance(scalar, (str, bool, int, float)) or scalar is None and key in value:
                result[key] = scalar
        errors = value.get("errors")
        if isinstance(errors, list):
            result["error_codes"] = sorted({
                str(error.get("code") or error.get("field"))
                for error in errors if isinstance(error, dict) and (error.get("code") or error.get("field"))
            })
        details = value.get("validation_details")
        if isinstance(details, dict) and isinstance(details.get("errors"), list):
            result["validation_error_codes"] = sorted({
                str(error.get("code") or error.get("field"))
                for error in details["errors"]
                if isinstance(error, dict) and (error.get("code") or error.get("field"))
            })
    elif isinstance(value, list):
        result["item_count"] = len(value)
    elif isinstance(value, str):
        result["character_count"] = len(value)
    return result


def _usage(response: object) -> dict[str, int] | None:
    if not isinstance(response, dict) or not isinstance(response.get("usage"), dict):
        return None
    usage = response["usage"]
    mapping = {
        "input_tokens": usage.get("prompt_tokens", usage.get("input_tokens")),
        "output_tokens": usage.get("completion_tokens", usage.get("output_tokens")),
        "total_tokens": usage.get("total_tokens"),
    }
    result = {key: value for key, value in mapping.items()
              if isinstance(value, int) and not isinstance(value, bool) and value >= 0}
    return result or None


class _Backend:
    def __init__(self, settings: ObservabilitySettings):
        from langfuse import Langfuse, propagate_attributes

        self.client = Langfuse(
            public_key=settings.public_key,
            secret_key=settings.secret_key,
            base_url=settings.base_url,
            tracing_enabled=True,
            environment=settings.environment,
        )
        self.propagate_attributes = propagate_attributes


_backend: _Backend | None = None
_backend_key: tuple[str | None, str | None, str, str] | None = None


def _get_backend(settings: ObservabilitySettings) -> _Backend | None:
    global _backend, _backend_key
    if not settings.enabled:
        return None
    if not settings.public_key or not settings.secret_key or not settings.base_url:
        LOGGER.warning("LogicWeaver observability is enabled but Langfuse credentials are incomplete")
        return None
    key = (settings.public_key, settings.secret_key, settings.base_url, settings.environment)
    if _backend is not None and _backend_key == key:
        return _backend
    try:
        _backend = _Backend(settings)
        _backend_key = key
        return _backend
    except Exception as exc:  # tracing must never become a business dependency
        LOGGER.warning("Langfuse initialization failed: %s", type(exc).__name__)
        return None


class Observation:
    def __init__(self, trace: "RunTrace", context_manager: object | None):
        self._trace = trace
        self._context_manager = context_manager
        self._span: object | None = None
        self._started = time.perf_counter()

    def __enter__(self) -> "Observation":
        if self._context_manager is not None:
            try:
                self._span = self._context_manager.__enter__()  # type: ignore[attr-defined]
            except Exception as exc:
                self._trace._telemetry_failure("start observation", exc)
        return self

    def update(self, *, output: object | None = None, metadata: dict | None = None,
               level: str | None = None, status_message: str | None = None,
               usage_details: dict[str, int] | None = None, model: str | None = None) -> None:
        if self._span is None:
            return
        values: dict[str, object] = {}
        if output is not None:
            values["output"] = self._trace.protect(output)
        if metadata is not None:
            values["metadata"] = llm_client.redact(metadata)
        if level is not None:
            values["level"] = level
        if status_message is not None:
            values["status_message"] = status_message
        if usage_details:
            values["usage_details"] = usage_details
        if model:
            values["model"] = model
        try:
            self._span.update(**values)  # type: ignore[attr-defined]
        except Exception as exc:
            self._trace._telemetry_failure("update observation", exc)

    def record_reply(self, reply: llm_client.LLMReply) -> None:
        response_model = reply.response.get("model") if isinstance(reply.response, dict) else None
        self.update(
            output={"raw_response": reply.response, "content": reply.content},
            usage_details=_usage(reply.response),
            model=response_model if isinstance(response_model, str) else None,
        )

    def __exit__(self, exc_type, exc, traceback) -> bool:
        if exc is not None:
            self.update(
                output=self._trace.error_payload(exc), level="ERROR",
                status_message=f"{type(exc).__name__} during {self._trace.active_step}",
            )
        self.update(metadata={"duration_ms": round((time.perf_counter() - self._started) * 1000, 3)})
        if self._context_manager is not None and self._span is not None:
            try:
                self._context_manager.__exit__(exc_type, exc, traceback)  # type: ignore[attr-defined]
            except Exception as telemetry_exc:
                self._trace._telemetry_failure("end observation", telemetry_exc)
        return False


class RunTrace:
    """A request-local trace whose every SDK operation is best-effort."""

    def __init__(self, *, case_id: str, case_title: str, stage: str, run_id: str,
                 workflow_id: str, created_at: str, initial_input: object | None = None):
        self.settings = load_settings()
        self.run_name = build_run_name(case_title, created_at, stage)
        self.external_run_name = self.run_name if self.settings.capture_content else build_run_name(
            f"案件 {hashlib.sha256(case_id.encode('utf-8')).hexdigest()[:8]}", created_at, stage)
        self.workflow_id = workflow_id
        self.active_step = "preparation"
        self._backend = _get_backend(self.settings)
        self._attribute_context: object | None = None
        self._root_context: object | None = None
        self._root: object | None = None
        self._failed = False
        self._started = time.perf_counter()
        if self._backend is None:
            return
        metadata = {"case_id": case_id, "stage": stage, "run_id": run_id, "workflow_id": workflow_id}
        try:
            self._attribute_context = self._backend.propagate_attributes(
                session_id=workflow_id, trace_name=self.external_run_name,
                tags=["logicweaver", stage.lower().replace(" ", "-")], metadata=metadata,
            )
            self._attribute_context.__enter__()  # type: ignore[attr-defined]
            self._root_context = self._backend.client.start_as_current_observation(
                as_type="chain", name=self.external_run_name,
                input=self.protect(initial_input or metadata), metadata=metadata,
            )
            self._root = self._root_context.__enter__()  # type: ignore[attr-defined]
        except Exception as exc:
            self._telemetry_failure("start trace", exc)
            self._close_contexts(None, None, None)

    def protect(self, value: object) -> object:
        redacted = llm_client.redact(value)
        return redacted if self.settings.capture_content else _summary(redacted)

    def error_payload(self, exc: BaseException, *, step: str | None = None) -> dict[str, object]:
        payload: dict[str, object] = {
            "exception_type": type(exc).__name__,
            "error_stage": step or self.active_step,
        }
        code = getattr(exc, "code", None)
        if isinstance(code, str):
            payload["error_code"] = code
        details = getattr(exc, "details", None)
        if details:
            payload["validation_details"] = details
        response = getattr(exc, "response", None)
        if response is not None:
            payload["provider_response"] = response
        if self.settings.capture_content:
            payload["message"] = str(exc)
        return payload

    def _telemetry_failure(self, action: str, exc: BaseException) -> None:
        if not self._failed:
            LOGGER.warning("Langfuse %s failed: %s", action, type(exc).__name__)
            self._failed = True

    @contextmanager
    def observation(self, name: str, *, as_type: str = "span", input: object | None = None,
                    metadata: dict | None = None, model: str | None = None,
                    model_parameters: dict | None = None) -> Iterator[Observation]:
        self.active_step = name
        context_manager = None
        if self._backend is not None and self._root is not None:
            try:
                context_manager = self._backend.client.start_as_current_observation(
                    as_type=as_type, name=name, input=self.protect(input),
                    metadata=llm_client.redact(metadata or {}), model=model,
                    model_parameters=model_parameters,
                )
            except Exception as exc:
                self._telemetry_failure("create observation", exc)
        observation = Observation(self, context_manager)
        with observation:
            yield observation

    def generation(self, name: str, *, config: llm_client.LLMConfig, prompt: str,
                   model_input: dict, schema: dict) -> Iterator[Observation]:
        request = llm_client.structured_request_payload(config, prompt, model_input, schema)
        return self.observation(
            name, as_type="generation", input=request, model=config.model,
            model_parameters={"max_completion_tokens": config.max_completion_tokens},
            metadata={"provider": config.provider, "base_url": config.base_url},
        )

    def finish_success(self, output: object | None = None) -> None:
        self._finish(output=output, error=None)

    def finish_error(self, exc: BaseException, *, step: str | None = None) -> None:
        self._finish(output=self.error_payload(exc, step=step), error=exc)

    def _finish(self, *, output: object | None, error: BaseException | None) -> None:
        if self._root is not None:
            try:
                values: dict[str, object] = {
                    "output": self.protect(output),
                    "metadata": {
                        "workflow_id": self.workflow_id,
                        "duration_ms": round((time.perf_counter() - self._started) * 1000, 3),
                    },
                }
                if error is not None:
                    values.update(level="ERROR", status_message=f"{type(error).__name__} during {self.active_step}")
                self._root.update(**values)  # type: ignore[attr-defined]
            except Exception as exc:
                self._telemetry_failure("finish trace", exc)
        self._close_contexts(type(error) if error else None, error, error.__traceback__ if error else None)

    def _close_contexts(self, exc_type, exc, traceback) -> None:
        for context in (self._root_context, self._attribute_context):
            if context is not None:
                try:
                    context.__exit__(exc_type, exc, traceback)  # type: ignore[attr-defined]
                except Exception as close_exc:
                    self._telemetry_failure("close trace", close_exc)
        self._root = None
        self._root_context = None
        self._attribute_context = None


def flush() -> None:
    """Best-effort flush for tests and short-lived processes."""
    if _backend is None:
        return
    try:
        _backend.client.flush()
    except Exception as exc:
        LOGGER.warning("Langfuse flush failed: %s", type(exc).__name__)
