"""Case A native Langfuse experiments. The default path is entirely offline."""
import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
from importlib.metadata import version
import json
from pathlib import Path
import shutil
import subprocess
import time
from types import SimpleNamespace
from uuid import uuid4

from app import investigation_agent as agent, llm_client, observability
from .data import ROOT, TaskInput, build_case, dataset_items, digest, execution_context_config, isolated_case, load_definition, load_snapshot, work_context
from .scoring import automatic_metrics, evaluator
from . import remote

REPO = ROOT.parents[1]


def selected_task_ids(task_id=None):
    ids = [item["task_id"] for item in load_definition()["items"]]
    if task_id is not None and task_id not in ids:
        raise ValueError(f"Unknown task: {task_id}")
    return [task_id] if task_id is not None else ids


def select_dataset_items(dataset, task_id=None):
    ids = selected_task_ids(task_id)
    dataset.items = [item for item in dataset.items if item.input["task_id"] in ids]
    if [item.input["task_id"] for item in dataset.items] != ids:
        raise ValueError("Selected Dataset items differ from the requested tasks")
    return dataset


def run_metadata(model, task_id=None):
    context_config = execution_context_config()
    prompts = {phase: agent.load_composed_prompt(name) for phase, name in agent.PROMPT_NAMES.items()}
    prompt = prompts["execution"]
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True, check=True).stdout.strip()
    dirty = bool(subprocess.run(["git", "status", "--porcelain=v1"], cwd=REPO, capture_output=True, text=True, check=True).stdout)
    return {**prompt.metadata(model), "prompt_manifest": {phase: {"phase": phase, **p.metadata(model)} for phase, p in prompts.items()},
            "code_git_commit": commit, "worktree_dirty": dirty,
            "dataset_version": load_definition()["version"], "dataset_content_sha256": digest(dataset_items()),
            "case_snapshot_id": load_snapshot()["snapshot_id"], "case_snapshot_sha256": load_snapshot()["sha256"],
            "case_data_sha256": digest(build_case().model_dump(mode="json")),
            "initial_state_sha256": digest(load_definition()["initial_state"]),
            "execution_context_version": context_config["version"],
            "execution_context_sha256": digest(context_config),
            "tool_contract_sha256": digest({phase: agent.tool_definitions(phase) for phase in agent.PROMPT_NAMES}), "langfuse_sdk_version": version("langfuse"),
            "max_model_calls_per_item": agent.MAX_MODEL_CALLS, "max_model_calls_three_items": 3 * agent.MAX_MODEL_CALLS,
            "selected_task_ids": selected_task_ids(task_id),
            "max_model_calls_this_run": len(selected_task_ids(task_id)) * agent.MAX_MODEL_CALLS,
            "max_concurrency": 1}


def make_task(client, *, allow_paid=False, offline=False, artifact_dir=None):
    if not offline and not allow_paid:
        raise ValueError("Real model execution requires explicit --allow-paid-models")
    canonical = {i["input"]["task_id"]: i for i in dataset_items()}

    async def task(*, item, **kwargs):
        values = item["input"] if isinstance(item, dict) else item.input
        parsed = TaskInput.model_validate(values)
        expected = canonical[parsed.task_id]
        if values != expected["input"]:
            raise ValueError("Evaluation input differs from the verified fixed task")
        parent_trace_id = client.get_current_trace_id()
        if not parent_trace_id or parent_trace_id == expected["source_trace_id"]:
            raise ValueError("A fresh native Experiment Trace is required; history cannot be reused")
        started = time.perf_counter()
        with isolated_case() as (case, case_dir):
            result, error = None, None
            try:
                context = work_context(case, parsed.task_id)
                result = await agent.run_investigation(case, parsed.goal, parsed.selected_info_unit_ids,
                    work_context=context, issue_key=context["current_issue"])
            except Exception as exc:
                # Keep the native experiment item and its Scores even on Agent failure.
                # The real RunTrace already contains the original error/audit.
                error = {"code": getattr(exc, "code", type(exc).__name__), "exception_type": type(exc).__name__}
                client.update_current_span(level="ERROR", status_message="Investigation execution failed", metadata={"runtime_error": error})
            audits = sorted((case_dir / "runs").glob("*.output.json"))
            if len(audits) > 1:
                raise ValueError("A task must execute exactly one full Investigation run")
            audit = json.loads(audits[0].read_text(encoding="utf-8")) if audits else {}
            if result is None:
                result = audit.get("result")
            if artifact_dir:
                destination = Path(artifact_dir) / parsed.task_id
                destination.mkdir(parents=True, exist_ok=False)
                if (case_dir / "runs").exists():
                    shutil.copytree(case_dir / "runs", destination / "runs")
            metrics, access = automatic_metrics(audit, result, time.perf_counter() - started, expected["expected_output"], error)
            return {"task_id": parsed.task_id, "mode": "dry-run" if offline else "live", "result": result,
                    "runtime_error": error, "metrics": metrics, "access": access,
                    "initial_case_data_sha256": digest(case.model_dump(mode="json")), "trace_id": parent_trace_id,
                    "semantic_quality_scored": False}
    return task


def _mock_reply(config, messages, definitions, schema=None, **kwargs):
    """Offline protocol exercise, explicitly NOT a simulated semantic benchmark."""
    from app.llm_client import ToolReply
    state = json.loads(messages[1]["content"])
    question = {"id": "wording", "question": "离线协议：核对来源记载", "status": "pending",
        "related_info_unit_ids": [], "evidence_refs": [], "finding": None, "block_reason": None, "block_kind": None}
    step = {"id": "read-source", "question_id": "wording", "objective": "核对可定位来源记载",
        "status": "pending", "result_note": None, "basis_info_unit_ids": []}
    if state["phase"] == "entry":
        name, args = "update_investigation_steps", {"questions": [question], "steps": [step]}
    elif state["remaining_budget"]["read"] == agent.MAX_TOOL_CALLS:
        name, args = "get_evidence", {"info_unit_ids": ["U004"], "question_ids": ["wording"], "step_id": "read-source"}
    else:
        name = "finish_investigation"
        refs = [{"info_unit_id": "U004", "source_ref_index": i} for i in (0, 1)]
        finding = "离线协议样例，仅核对可定位来源，不代表完成真实调查。"
        args = {"answer": finding, "findings": [{"statement": finding, "info_unit_ids": ["U004"], "relation_ids": [], "evidence_refs": refs}],
            "completion_status": "completed", "step_updates": [{**step, "status": "completed", "result_note": finding}],
            "question_updates": [{**question, "status": "answered",
                "related_info_unit_ids": ["U004"], "evidence_refs": refs, "finding": finding,
                "block_reason": None, "block_kind": None}]}
    call = {"id": uuid4().hex, "type": "function", "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}
    message = {"role": "assistant", "content": None, "tool_calls": [call]}
    return ToolReply(content="", message=message, response={"model": config.model, "choices": [{"message": message, "finish_reason": "tool_calls"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "completion_tokens_details": {"reasoning_tokens": 0}}})


def dry_run(*, task_id=None):
    """Real SDK v5 experiment + real Agent; memory-only spans and denied network."""
    import httpx
    from unittest.mock import patch, AsyncMock
    from langfuse import Langfuse, propagate_attributes
    from langfuse.api import Dataset, DatasetItem
    from langfuse._client.datasets import DatasetClient
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    calls = []
    def transport(request):
        calls.append((request.method, request.url.path))
        if request.url.path.endswith("/projects"):
            return httpx.Response(200, json={"data": [{"id": "dry-project", "name": "offline",
                "organization": {"id": "dry-org", "name": "offline"}, "metadata": {}}]})
        if request.method == "POST" and request.url.path.endswith("/scores"):
            return httpx.Response(202, json={"message": "Accepted"})
        raise AssertionError("Offline Langfuse transport denied unexpected API operation")

    exporter, provider = InMemorySpanExporter(), TracerProvider()
    http = httpx.Client(transport=httpx.MockTransport(transport))
    client = Langfuse(public_key="pk-dry-" + uuid4().hex, secret_key="offline-not-a-secret", base_url="https://offline.invalid",
        tracer_provider=provider, span_exporter=exporter, httpx_client=http, environment="evaluation-dry-run")
    stamp = datetime.now(timezone.utc)
    definition = load_definition()
    dataset = DatasetClient(dataset=Dataset(id="dry-dataset", name=definition["name"], metadata={}, project_id="dry-project",
        created_at=stamp, updated_at=stamp), items=[DatasetItem(**i, status="ACTIVE", dataset_id="dry-dataset",
        dataset_name=definition["name"], created_at=stamp, updated_at=stamp, media_references=[]) for i in dataset_items()],
        langfuse_client=client, version=stamp)
    select_dataset_items(dataset, task_id)
    config = llm_client.LLMConfig("openai", "https://offline.invalid/v1", "offline-mock", "unused", max_completion_tokens=20000)
    settings = observability.ObservabilitySettings(True, True, "offline", "unused", "https://offline.invalid", "evaluation-dry-run")
    backend = SimpleNamespace(client=client, propagate_attributes=propagate_attributes)
    try:
        with ExitStack() as stack:
            stack.enter_context(patch.object(observability, "load_settings", return_value=settings))
            stack.enter_context(patch.object(observability, "_get_backend", return_value=backend))
            stack.enter_context(patch.object(llm_client, "load_config", return_value=config))
            stack.enter_context(patch.object(llm_client, "call_tools", new=AsyncMock(side_effect=_mock_reply)))
            stack.enter_context(patch.object(llm_client, "_post_completion", new=AsyncMock(side_effect=AssertionError("Paid transport forbidden in dry-run"))))
            result = dataset.run_experiment(name="offline-protocol-check", task=make_task(client, offline=True),
                evaluators=[evaluator], max_concurrency=1, metadata=run_metadata(config.model, task_id))
            client.flush()
        spans = exporter.get_finished_spans()
        summaries = [{"trace_id": format(s.context.trace_id, "032x"), "name": s.name,
                      "parent_span_id": format(s.parent.span_id, "016x") if s.parent else None} for s in spans]
        if [r.output["task_id"] for r in result.item_results] != selected_task_ids(task_id) or any(
                not r.evaluations or not r.output["metrics"]["runtime_finish_validated"] for r in result.item_results):
            raise ValueError("Offline native experiment or Agent protocol check failed")
        for item in result.item_results:
            own = [s for s in summaries if s["trace_id"] == item.trace_id]
            if not any(s["name"] == "experiment-item-run" and s["parent_span_id"] is None for s in own):
                raise ValueError("Experiment root Trace missing")
            for name in ("Investigation Agent · model call", "Investigation Agent · tool call", "Investigation Agent · final working state"):
                if not any(s["name"] == name and s["parent_span_id"] for s in own):
                    raise ValueError(f"Existing Agent node is not in the experiment Trace: {name}")
        return {"mode": "dry-run", "remote_synchronized": False, "paid_model_calls": 0, "sdk_mock_http_calls": len(calls),
                "items": [{"task_id": r.output["task_id"], "metrics": r.output["metrics"], "initial_case_data_sha256": r.output["initial_case_data_sha256"]} for r in result.item_results],
                "span_count": len(spans), "trace_ids": [r.trace_id for r in result.item_results], "semantic_quality_scored": False,
                "maximum_live_model_calls": len(selected_task_ids(task_id)) * agent.MAX_MODEL_CALLS}
    finally:
        client.shutdown()
        http.close()


def live(*, allow_paid=False, task_id=None):
    if not allow_paid:
        raise ValueError("live requires --allow-paid-models; no model was called")
    settings = observability.load_settings()
    if not settings.enabled or not settings.capture_content:
        raise ValueError("Live evaluation requires enabled Langfuse content capture for evidence review")
    backend = observability._get_backend(settings)
    if backend is None:
        raise ValueError("Langfuse backend unavailable; no model was called")
    client, config = backend.client, llm_client.load_config()
    metadata = run_metadata(config.model, task_id)
    if metadata["worktree_dirty"]:
        raise ValueError("Live evaluation requires a clean committed checkout")
    remote.project_id(client)
    since = datetime.now(timezone.utc)
    dataset = remote.validate_remote_dataset(client.get_dataset(load_definition()["name"], version=since))
    select_dataset_items(dataset, task_id)
    metadata.update(dataset_id=dataset.id, dataset_server_version=since.isoformat(),
                    max_completion_tokens=config.max_completion_tokens)
    output_dir = ROOT / "results" / "investigation" / uuid4().hex
    output_dir.mkdir(parents=True, exist_ok=False)
    result = dataset.run_experiment(name=f"case-a-{metadata['code_git_commit'][:8]}-{metadata['prompt_version']}",
        run_name=f"case-a-{metadata['code_git_commit'][:8]}-{uuid4().hex}", metadata=metadata,
        task=make_task(client, allow_paid=True, artifact_dir=output_dir), evaluators=[evaluator], max_concurrency=1)
    execution = {"mode": "live", "experiment_id": result.experiment_id, "experiment_url": result.experiment_url,
        "remote_verified": False, "audit_directory": str(output_dir), "metadata": metadata,
        "since": since.isoformat(),
        "items": [{"task_id": r.item.input["task_id"], "dataset_item_id": r.item.id, "trace_id": r.trace_id,
                   "metrics": r.output["metrics"] if isinstance(r.output, dict) else None} for r in result.item_results]}
    manifest = output_dir / "execution.json"
    manifest.write_text(json.dumps(execution, ensure_ascii=False, indent=2), encoding="utf-8")
    if [item["task_id"] for item in execution["items"]] != selected_task_ids(task_id):
        raise ValueError(f"Native SDK did not retain exactly the requested tasks; inspect Langfuse and {output_dir}")
    client.flush()
    remote.verify_with_bounded_wait(lambda: remote.verify_experiment(client, result, since))
    execution["remote_verified"] = True
    manifest.write_text(json.dumps(execution, ensure_ascii=False, indent=2), encoding="utf-8")
    return execution


def score_history(client):
    trace_id, audit, result, elapsed = remote.historical_audit(client)
    metrics, _ = automatic_metrics(audit, result, elapsed, {"evidence_groups": []})
    ids = remote.store_scores(client, trace_id, metrics,
        metadata={"task_id": "A1", "historical": True, "not_an_experiment": True, "snapshot_differs_from_fixed_dataset": True},
        comment="Historical execution-only audit. Not semantic task completion; no fixed-snapshot evidence coverage comparison.")
    remote.verify_with_bounded_wait(lambda: remote.verify_scores(client, trace_id, metrics))
    return {"trace_id": trace_id, "score_ids": ids, "metrics": metrics, "remote_verified": True, "new_experiment_created": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", nargs="?", default="dry-run", choices=["dry-run", "init", "live", "score-history"])
    parser.add_argument("--allow-paid-models", action="store_true", help="Explicit paid-model authorization, only for live")
    parser.add_argument("--task", choices=["A1", "A2", "A3"], help="Run only this item; default: all three tasks")
    args = parser.parse_args(argv)
    if args.task and args.operation not in {"dry-run", "live"}:
        parser.error("--task applies only to dry-run and live")
    if not version("langfuse").startswith("5."):
        parser.error("This integration requires the reviewed Langfuse Python SDK v5")
    try:
        if args.operation == "dry-run":
            result = dry_run(task_id=args.task)
        elif args.operation == "live":
            result = live(allow_paid=args.allow_paid_models, task_id=args.task)
        elif args.operation == "init":
            result = remote.initialize(remote.remote_client())
        else:
            result = score_history(remote.remote_client())
    except Exception as exc:
        parser.exit(1, f"Evaluation failed ({type(exc).__name__}); remote synchronization is NOT confirmed. {exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
