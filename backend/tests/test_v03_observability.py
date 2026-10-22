from contextlib import nullcontext
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from app import case_store, llm_client, observability, run_index
from app.prompt_loader import LoadedPrompt


SETTINGS = observability.ObservabilitySettings(
    enabled=True,
    capture_content=True,
    public_key="pk-test",
    secret_key="sk-test",
    base_url="https://cloud.langfuse.com",
    environment="test",
)
CONFIG = llm_client.LLMConfig(
    "azure", "https://test.services.ai.azure.com/openai/v1", "mock-model", "secret")
PROMPT = LoadedPrompt(
    name="extraction", content="system prompt", version="a" * 64,
    git_commit="b" * 40, schema_version="0.3",
)


class FakeSpan:
    def __init__(self, start):
        self.start = start
        self.updates = []

    def update(self, **kwargs):
        self.updates.append(kwargs)


class FakeContext:
    def __init__(self, value=None):
        self.value = value

    def __enter__(self):
        return self.value

    def __exit__(self, *_args):
        return False


class FakeClient:
    def __init__(self):
        self.spans = []

    def start_as_current_observation(self, **kwargs):
        span = FakeSpan(kwargs)
        self.spans.append(span)
        return FakeContext(span)


class FakeBackend:
    def __init__(self):
        self.client = FakeClient()
        self.attributes = []

    def propagate_attributes(self, **kwargs):
        self.attributes.append(kwargs)
        return nullcontext()


class TestObservability(TestCase):
    def test_trace_records_structure_usage_validation_and_workflow(self):
        backend = FakeBackend()
        with patch.object(observability, "load_settings", return_value=SETTINGS), patch.object(
                observability, "_get_backend", return_value=backend):
            trace = observability.RunTrace(
                case_id="case-1", case_title="租赁纠纷", stage="Extraction",
                run_id="run-1", workflow_id="workflow-1", created_at="2026-10-22T10:00:00+08:00",
                initial_input={"case_id": "case-1"},
            )
            with trace.generation(
                    "Extraction · model call", config=CONFIG, prompt=PROMPT,
                    model_input={"files": ["正文"]}, schema={"type": "object"}) as generation:
                generation.record_reply(llm_client.LLMReply(
                    "{\"ok\":true}", {"model": "mock-model", "usage": {
                        "prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14,
                    }},
                ))
            with trace.observation(
                    "Extraction · validate", as_type="guardrail", input={"candidate": 1}) as validation:
                validation.update(output={"valid": True})
            trace.finish_success({"status": "success"})

        self.assertEqual(backend.attributes[0]["session_id"], "workflow-1")
        self.assertEqual([span.start["name"] for span in backend.client.spans], [
            "租赁纠纷 · 2026-10-22 10:00:00 · Extraction",
            "Extraction · model call",
            "Extraction · validate",
        ])
        generation_update = backend.client.spans[1].updates[0]
        generation_input = backend.client.spans[1].start["input"]
        self.assertEqual(json.loads(generation_input["messages"][1]["content"]), {"files": ["正文"]})
        self.assertEqual(
            generation_input["response_format"]["json_schema"]["schema"], {"type": "object"})
        self.assertEqual(backend.client.spans[1].start["version"], PROMPT.version)
        self.assertEqual(backend.client.spans[1].start["metadata"]["prompt_git_commit"], PROMPT.git_commit)
        self.assertEqual(backend.client.spans[1].start["metadata"]["schema_version"], "0.3")
        self.assertEqual(generation_update["usage_details"]["total_tokens"], 14)
        self.assertEqual(generation_update["output"]["content"], "{\"ok\":true}")
        self.assertIn("duration_ms", backend.client.spans[1].updates[-1]["metadata"])
        self.assertEqual(backend.client.spans[2].updates[0]["output"], {"valid": True})

    def test_default_privacy_summary_and_disabled_mode(self):
        private = SETTINGS.__class__(**{**SETTINGS.__dict__, "capture_content": False})
        backend = FakeBackend()
        with patch.object(observability, "load_settings", return_value=private), patch.object(
                observability, "_get_backend", return_value=backend):
            trace = observability.RunTrace(
                case_id="case-1", case_title="案件", stage="Relation", run_id="run-1",
                workflow_id="workflow-1", created_at="2026-10-22T10:00:00+08:00",
                initial_input={"body": "张某身份证号"},
            )
            protected = trace.protect({"body": "张某身份证号"})
            trace.finish_success({"body": "张某身份证号"})
        self.assertFalse(protected["content_captured"])
        self.assertNotIn("张某", str(protected))
        self.assertNotEqual(backend.client.spans[0].start["name"], trace.run_name)

        disabled = SETTINGS.__class__(**{**SETTINGS.__dict__, "enabled": False})
        with patch.object(observability, "load_settings", return_value=disabled), patch.object(
                observability, "_get_backend", return_value=None) as get_backend:
            trace = observability.RunTrace(
                case_id="case-1", case_title="案件", stage="Relation", run_id="run-2",
                workflow_id="workflow-2", created_at="2026-10-22T10:00:00+08:00",
            )
            trace.finish_success({"ok": True})
        get_backend.assert_called_once()

    def test_business_error_and_telemetry_failure_are_isolated(self):
        backend = FakeBackend()
        with patch.object(observability, "load_settings", return_value=SETTINGS), patch.object(
                observability, "_get_backend", return_value=backend):
            trace = observability.RunTrace(
                case_id="case-1", case_title="案件", stage="Relation", run_id="run-1",
                workflow_id="workflow-1", created_at="2026-10-22T10:00:00+08:00",
            )
            error = llm_client.LLMFailure("llm_call_failed", "mock failure")
            trace.finish_error(error, step="Relation · model call")
        root_update = backend.client.spans[0].updates[-1]
        self.assertEqual(root_update["level"], "ERROR")
        self.assertEqual(root_update["output"]["error_code"], "llm_call_failed")
        self.assertEqual(root_update["output"]["error_stage"], "Relation · model call")

        class BrokenBackend(FakeBackend):
            def __init__(self):
                super().__init__()
                self.client.start_as_current_observation = lambda **_kwargs: (_ for _ in ()).throw(
                    RuntimeError("telemetry unavailable"))

        with patch.object(observability, "load_settings", return_value=SETTINGS), patch.object(
                observability, "_get_backend", return_value=BrokenBackend()):
            trace = observability.RunTrace(
                case_id="case-1", case_title="案件", stage="Extraction", run_id="run-2",
                workflow_id="workflow-2", created_at="2026-10-22T10:00:00+08:00",
            )
            trace.finish_success({"business": "still succeeds"})


class TestRunIndex(TestCase):
    def test_filters_and_locates_existing_json_artifacts(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / "case-1"
            (directory / "runs").mkdir(parents=True)
            run = {
                "id": "run-1", "stage": "relation", "status": "failed", "model": "mock",
                "prompt_version": "v1", "input_snapshot_path": "runs/run-1.input.json",
                "output_snapshot_path": "runs/run-1.output.json", "error_message": "failed",
                "created_at": "2026-10-22T10:00:00+08:00",
            }
            (directory / "case.json").write_text(json.dumps({
                "case_id": "case-1", "case_title": "租赁纠纷", "stage_runs": [run],
            }, ensure_ascii=False), encoding="utf-8")
            for name, payload in {
                "run-1.input.json": {"run_id": "run-1"},
                "run-1.output.json": {
                    "run_id": "run-1", "run_name": "租赁纠纷 · 2026-10-22 10:00:00 · Relation",
                    "workflow_id": "workflow-1",
                },
                "run-1.stage.json": run,
            }.items():
                (directory / "runs" / name).write_text(
                    json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            with patch.object(case_store, "DATA_DIR", root):
                entries = run_index.collect_runs(
                    case_filter="租赁", stage="relation", status="failed", since="2026-10-22")
                excluded = run_index.collect_runs(status="success")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].workflow_id, "workflow-1")
        self.assertTrue(entries[0].output_path.endswith("run-1.output.json"))
        self.assertEqual(excluded, [])
