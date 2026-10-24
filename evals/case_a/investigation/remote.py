"""Native Langfuse v5 Dataset/Score setup and explicit remote verification."""
from datetime import datetime, timezone
import json
import time

from langfuse import Langfuse
from langfuse.api.core.api_error import ApiError
from langfuse.api.scores.types.create_score_request import CreateScoreRequest

from app.observability import load_settings
from .data import TaskInput, Criteria, dataset_items, digest, load_definition, load_snapshot


def remote_client(*, tracing=False):
    settings = load_settings()
    if not settings.public_key or not settings.secret_key:
        raise ValueError("Langfuse project credentials are required for this explicit remote operation")
    return Langfuse(public_key=settings.public_key, secret_key=settings.secret_key, base_url=settings.base_url,
                    environment=settings.environment, tracing_enabled=tracing)


def project_id(client):
    projects = client.api.projects.get().data
    if len(projects) != 1:
        raise ValueError("Expected credentials scoped to exactly one Langfuse project")
    return projects[0].id


def pages(fetch, **kwargs):
    result, page = [], 1
    while True:
        response = fetch(page=page, limit=100, **kwargs)
        result.extend(response.data)
        if len(response.data) < 100:
            return result
        page += 1


def score_config_specs():
    return {name: {"name": name, "data_type": "NUMERIC", "min_value": 0.0, "max_value": 2.0,
                   "description": "Case A Investigation v1. " + "; ".join(f"{i}: {label}" for i, label in enumerate(labels))
                       + ". Comment must cite findings, source pointers or tool/Question evidence. No unvalidated total-score threshold."}
            for name, labels in load_definition()["human_scores"].items()}


def ensure_score_configs(client):
    existing = pages(client.api.score_configs.get)
    result = {}
    for name, spec in score_config_specs().items():
        matches = [c for c in existing if c.name == name and not c.is_archived]
        if len(matches) > 1:
            raise ValueError(f"Ambiguous active Score Config: {name}")
        if matches:
            config = matches[0]
            if any(getattr(config, k) != v for k, v in spec.items()):
                raise ValueError(f"Score Config conflicts with the reviewed rubric: {name}; no overwrite performed")
        else:
            config = client.api.score_configs.create(**spec)
        checked = client.api.score_configs.get_by_id(config.id)
        if any(getattr(checked, k) != v for k, v in spec.items()):
            raise ValueError(f"Remote Score Config verification failed: {name}")
        result[name] = checked.id
    return result


def validate_remote_dataset(dataset):
    expected = dataset_items()
    by_id = {item.id: item for item in dataset.items}
    if set(by_id) != {item["id"] for item in expected} or len(dataset.items) != 3:
        raise ValueError("Remote dataset must contain exactly the three active reviewed items")
    for item in expected:
        actual = by_id[item["id"]]
        if actual.status != "ACTIVE":
            raise ValueError("Reviewed dataset items must remain active")
        for field in ("input", "expected_output", "metadata", "source_trace_id", "source_observation_id"):
            if getattr(actual, field) != item[field]:
                raise ValueError(f"Remote dataset item drift: {item['input']['task_id']}.{field}")
        TaskInput.model_validate(actual.input)
        Criteria.model_validate(actual.expected_output)
    dataset.items = [by_id[item["id"]] for item in expected]
    return dataset


def initialize(client):
    definition, snapshot, items = load_definition(), load_snapshot(), dataset_items()
    pid = project_id(client)  # Fail before claiming successful synchronization.
    metadata = {"dataset_version": definition["version"], "case_snapshot_sha256": snapshot["sha256"],
                "definition_sha256": digest(definition), "criteria_kind": "human_semantic_rubric_not_exact_text"}
    try:
        dataset = client.get_dataset(definition["name"])
    except ApiError as exc:
        if exc.status_code != 404:
            raise
        client.create_dataset(name=definition["name"], description=definition["description"], metadata=metadata,
            input_schema=TaskInput.model_json_schema(), expected_output_schema=Criteria.model_json_schema())
        dataset = client.get_dataset(definition["name"])
    if dataset.metadata != metadata or dataset.input_schema != TaskInput.model_json_schema() or dataset.expected_output_schema != Criteria.model_json_schema():
        raise ValueError("Existing dataset identity/schema differs; create a reviewed new version instead of overwriting")
    existing = {item.id: item for item in dataset.items}
    for item in items:
        if item["id"] in existing:
            if any(getattr(existing[item["id"]], key) != item[key] for key in (
                    "input", "expected_output", "metadata", "source_trace_id", "source_observation_id")):
                raise ValueError(f"Existing item differs: {item['input']['task_id']}; not overwritten")
        else:
            client.create_dataset_item(dataset_name=definition["name"], **item)
    dataset = validate_remote_dataset(client.get_dataset(definition["name"]))
    configs = ensure_score_configs(client)
    return {"dataset_name": dataset.name, "dataset_id": dataset.id, "project_id": pid,
            "dataset_url": f"{load_settings().base_url}/project/{pid}/datasets/{dataset.id}",
            "dataset_version": definition["version"], "case_snapshot_sha256": snapshot["sha256"],
            "items": [{"task_id": i.input["task_id"], "id": i.id, "source_trace_id": i.source_trace_id} for i in dataset.items],
            "human_score_configs": configs, "remote_verified": True, "llm_calls": 0}


def read_trace(client, trace_id, start, end):
    rows, cursor = [], None
    while True:
        response = client.api.observations.get_many(trace_id=trace_id, from_start_time=start, to_start_time=end,
            fields="core,basic,io,metadata,model,usage", limit=1000, cursor=cursor)
        rows.extend(response.data)
        cursor = response.meta.cursor
        if not cursor:
            break
    if not rows:
        raise ValueError(f"No remote observations found for {trace_id}")
    return rows


def io_value(value):
    return json.loads(value) if isinstance(value, str) else value


def historical_audit(client):
    """Original history stays original; its InfoUnit IDs differ from fixed Gold."""
    trace_id = dataset_items()[0]["source_trace_id"]
    rows = read_trace(client, trace_id, datetime(2026, 10, 24, tzinfo=timezone.utc), datetime(2026, 10, 25, tzinfo=timezone.utc))
    roots = [o for o in rows if not o.parent_observation_id]
    if len(roots) != 1:
        raise ValueError("History must have one actual root")
    root = roots[0]
    result = io_value(root.output)
    calls, generations, knowledge = [], [], {}
    for row in sorted(rows, key=lambda r: r.start_time):
        inp, out = io_value(row.input), io_value(row.output)
        if row.type == "TOOL":
            calls.append({"name": inp["name"], "arguments": io_value(inp["arguments"]), "result": out})
        elif row.type == "GENERATION":
            generations.append({"raw_response": out["raw_response"]})
        elif row.name == "Investigation Agent · final working state":
            knowledge = inp["knowledge_state"]
    if not root.end_time or not isinstance(result, dict):
        raise ValueError("History has no completed audit window/output")
    return trace_id, {"tool_calls": calls, "model_calls": generations, "knowledge_state": knowledge}, result, (root.end_time - root.start_time).total_seconds()


def store_scores(client, trace_id, metrics, *, metadata, comment):
    """Synchronous official Score API, deterministic IDs; never fake ingestion success."""
    requests = [CreateScoreRequest(id=digest([trace_id, "case-a-investigation-execution-v1", name]), trace_id=trace_id,
                name=name, value=value, data_type="NUMERIC", comment=comment, metadata=metadata) for name, value in metrics.items()]
    existing = {s.id: s for s in read_scores(client, trace_id)}
    pending = []
    for request in requests:
        old = existing.get(request.id)
        if old and (old.name != request.name or old.value != request.value):
            raise ValueError("An existing historical score differs; no overwrite performed")
        if not old:
            pending.append(request)
    if pending:
        response = client.api.scores.create(request=pending)
        if getattr(response, "rejected", 0):
            raise ValueError(f"Langfuse rejected {response.rejected} automatic Scores")
    return [r.id for r in requests]


def read_scores(client, trace_id):
    rows, cursor = [], None
    while True:
        response = client.api.scores.get_many(trace_id=trace_id, limit=100, cursor=cursor)
        rows.extend(response.data)
        cursor = response.meta.cursor
        if not cursor:
            return rows


def verify_scores(client, trace_id, metrics):
    scores = read_scores(client, trace_id)
    found = {s.name: s.value for s in scores}
    if any(found.get(name) != value for name, value in metrics.items()):
        raise ValueError(f"Scores not yet verified remotely for trace {trace_id}")


def verify_experiment(client, result, since):
    """Check native experiment/item linkage AND the existing nested Agent tree."""
    items = client.api.experiments.list_items(experiment_id=result.experiment_id, from_start_time=since,
                                             fields="core", limit=1000).data
    remote_ids = {(row.trace_id, row.experiment_item_id, row.experiment_id, row.experiment_dataset_id) for row in items}
    for item_result in result.item_results:
        if not item_result.trace_id or (item_result.trace_id, item_result.item.id, result.experiment_id, item_result.item.dataset_id) not in remote_ids:
            raise ValueError("Native Experiment / Dataset Item / Trace association was not verified")
        rows = read_trace(client, item_result.trace_id, since, datetime.now(timezone.utc))
        if not any(r.name == "experiment-item-run" for r in rows) or not any(r.name.endswith("Investigation Agent") for r in rows):
            raise ValueError("Agent observations are not nested in the native experiment Trace")
        metrics = item_result.output["metrics"]
        if sum(r.name == "Investigation Agent · model call" for r in rows) != metrics["model_call_count"] or sum(
                r.name == "Investigation Agent · tool call" for r in rows) != metrics["tool_attempt_count"]:
            raise ValueError("Experiment lost original model/tool audit observations")
        if not any(r.name == "Investigation Agent · final working state" for r in rows) and not item_result.output.get("runtime_error"):
            raise ValueError("Experiment is missing the final Agent state")
        verify_scores(client, item_result.trace_id, item_result.output["metrics"])


def verify_with_bounded_wait(check):
    # Score ingestion is asynchronous even when the API accepted the batch.
    delays = (0, 1, 2, 4, 8)
    for delay in delays:
        if delay:
            time.sleep(delay)
        try:
            return check()
        except ValueError:
            if delay == delays[-1]:
                raise
