"""Deterministic execution/ACCESS metrics. No semantic pass/fail or LLM judge."""
from langfuse import Evaluation


def automatic_metrics(audit, result, elapsed_seconds, criteria, error=None):
    if not audit:
        return {"runtime_finish_validated": 0, "runtime_failure": 1, "audit_available": 0,
                "duration_seconds": elapsed_seconds, "token_usage_complete": 0}, {
            "meaning": "Audit unavailable; tool/model counts and source access are unknown."}
    calls, generations = audit.get("tool_calls", []), audit.get("model_calls", [])
    finish = [c for c in calls if c.get("name") == "finish_investigation"]
    failed = lambda c: c.get("result", {}).get("status") in {"error", "invalid_request"}
    result = result or {}
    metrics = {
        "audit_available": 1,
        "runtime_finish_validated": int(result.get("stop_reason") == "finish_investigation" and result.get("summary_status") == "completed"),
        "runtime_failure": int(error is not None or result.get("summary_status") == "failed"),
        "tool_error_count": sum(failed(c) for c in calls),
        "tool_attempt_count": len(calls),
        "finish_attempt_count": len(finish), "finish_failure_count": sum(failed(c) for c in finish),
        "no_progress_limit": int((result.get("stop_reason") or audit.get("stop_reason")) == "no_progress_limit"),
        "model_call_count": len(generations), "duration_seconds": elapsed_seconds,
    }
    explicit, provided = set(), set()
    for c in calls:
        if c.get("name") == "get_evidence" and not failed(c):
            explicit.update((r["info_unit_id"], r["source_ref_index"]) for r in c["result"].get("items", []))
    knowledge = audit.get("knowledge_state", {})
    provided.update((r["info_unit_id"], r["source_ref_index"]) for r in knowledge.get("source_refs", {}).values())
    provided.update(explicit)
    units = {uid for uid, _ in provided}
    metrics.update(read_info_unit_count=len({uid for uid, _ in explicit}), read_source_count=len(explicit),
                   provided_info_unit_count=len(units), provided_source_count=len(provided))
    for group in criteria["evidence_groups"]:
        matches = [uid in units for uid in group["unit_ids"]]
        metrics["evidence_access_" + group["id"]] = int(all(matches) if group["mode"] == "all" else any(matches))
    usage = [(g.get("raw_response") or {}).get("usage") for g in generations]
    complete = all(isinstance(u, dict) and all(isinstance(u.get(k), int) and not isinstance(u[k], bool) and u[k] >= 0
                   for k in ("prompt_tokens", "completion_tokens", "total_tokens")) for u in usage)
    metrics["token_usage_complete"] = int(complete)
    if complete:
        metrics.update({name: sum(u[key] for u in usage) for name, key in (
            ("input_tokens", "prompt_tokens"), ("output_tokens", "completion_tokens"), ("total_tokens", "total_tokens"))})
    if all(isinstance(u, dict) and isinstance(u.get("completion_tokens_details", {}).get("reasoning_tokens"), int) for u in usage):
        metrics["reasoning_tokens"] = sum(u["completion_tokens_details"]["reasoning_tokens"] for u in usage)
    access = {"read_source_refs": [{"info_unit_id": uid, "source_ref_index": index} for uid, index in sorted(explicit)],
              "provided_source_refs": [{"info_unit_id": uid, "source_ref_index": index} for uid, index in sorted(provided)],
              "summary_info_unit_ids": sorted(knowledge.get("info_units", {})),
              "meaning": "Access/provision only. Not correct understanding, use, independent support or task completion."}
    return metrics, access


def evaluator(*, output, **kwargs):
    return [Evaluation(name=name, value=value, data_type="NUMERIC",
        comment="Deterministic runtime/access metric; NOT a semantic task-quality judgment.",
        metadata={"task_id": output["task_id"], "scope": "execution_and_access_only"})
        for name, value in output["metrics"].items()]
