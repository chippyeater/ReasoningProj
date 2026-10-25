"""Native Langfuse v5 integration, independent Case A tasks, no paid models."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "backend"))

from langfuse.api.core.api_error import ApiError
from langfuse.api.scores.types.create_score_request import CreateScoreRequest
from app import case_store, llm_client
from evals.case_a.investigation import data, remote, runner, scoring


class FakeRemote:
    """Only the SDK/API boundary is mocked; definitions/validation stay real."""
    def __init__(self):
        self.dataset = None
        self.created_items, self.configs, self.scores = [], [], []
        self.api = NS(projects=NS(get=lambda: NS(data=[NS(id="project-test")])),
            score_configs=NS(get=lambda **kw: NS(data=self.configs), create=self.create_config,
                             get_by_id=lambda ident: next(c for c in self.configs if c.id == ident)),
            scores=NS(create=self.create_scores, get_many=lambda **kw: NS(data=self.scores, meta=NS(cursor=None))))

    def get_dataset(self, name, **kwargs):
        if not self.dataset:
            raise ApiError(status_code=404)
        return self.dataset

    def create_dataset(self, **kwargs):
        self.dataset = NS(id="dataset-test", project_id="project-test", items=[], **kwargs)

    def create_dataset_item(self, **kwargs):
        kwargs.pop("dataset_name")
        self.created_items.append(deepcopy(kwargs))
        self.dataset.items.append(NS(status="ACTIVE", **kwargs))

    def create_config(self, **kwargs):
        value = NS(id=f"config-{len(self.configs)}", is_archived=False, **kwargs)
        self.configs.append(value)
        return value

    def create_scores(self, *, request):
        self.scores.extend(request)
        return NS(accepted=len(request), rejected=0)


class TestInvestigationEval(unittest.TestCase):
    def test_definition_is_three_tasks_with_semantic_gold_and_no_call_quota(self):
        items = data.dataset_items()
        self.assertEqual([r["input"]["task_id"] for r in items], ["A1", "A2", "A3"])
        self.assertEqual(items[0]["source_trace_id"], "7a661b732c21ac28903cff5155755943")
        self.assertEqual(len({r["input"]["initial_state_sha256"] for r in items}), 1)
        for item in items:
            data.TaskInput.model_validate(item["input"])
            data.Criteria.model_validate(item["expected_output"])
            self.assertTrue(item["expected_output"]["critical_errors"])
            self.assertTrue(any(not g["necessary"] for g in item["expected_output"]["evidence_groups"]))
        self.assertIn("月租3000元", "".join(items[0]["expected_output"]["acceptable_boundaries"]))

    def test_gold_rejects_unknown_evidence_groups_and_bad_rubrics(self):
        definition = data.load_definition()
        for change in (lambda d: d["items"][0]["expected_output"]["evidence_groups"][0].update(unit_ids=["foreign"]),
                       lambda d: d["human_scores"].update(task_coverage=["0", "1"]),
                       lambda d: d["items"][0]["expected_output"].update(unreviewed_threshold=1)):
            with self.subTest(change=change), TemporaryDirectory() as directory:
                candidate = deepcopy(definition)
                change(candidate)
                path = Path(directory) / "definition.json"
                path.write_text(json.dumps(candidate), encoding="utf-8")
                with patch.object(data, "DEFINITION", path), self.assertRaises(ValueError):
                    data.dataset_items()

    def test_snapshot_tampering_is_rejected_without_touching_fixed_material(self):
        snapshot = data.load_snapshot()
        snapshot["files"]["gold/gold_info_units.jsonl"] = "0" * 64
        snapshot["sha256"] = data.digest({k: v for k, v in snapshot.items() if k != "sha256"})
        with TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            path.write_text(json.dumps(snapshot), encoding="utf-8")
            with patch.object(data, "SNAPSHOT", path), self.assertRaisesRegex(ValueError, "snapshot changed"):
                data.load_snapshot()

    def test_three_isolated_cases_are_identical_and_cannot_modify_original_files(self):
        original = case_store.DATA_DIR
        hashes, roots = [], []
        for _ in range(3):
            with data.isolated_case() as (case, root):
                roots.append(root)
                hashes.append(data.digest(case.model_dump(mode="json")))
                self.assertNotEqual(case_store.DATA_DIR, original)
                self.assertEqual(len(case.info_units), 17)
                self.assertFalse(case.stage_runs)
                self.assertFalse((root / "issue_workspaces.json").exists())
            self.assertEqual(case_store.DATA_DIR, original)
        self.assertEqual(len(set(hashes)), 1)
        self.assertEqual(len(set(roots)), 3)

    def test_isolation_rejects_non_audit_writes_and_restores_data_root(self):
        original = case_store.DATA_DIR
        with self.assertRaisesRegex(ValueError, "Only isolated run audits"):
            with data.isolated_case() as (_, root):
                (root / "issue_workspaces.json").write_text("{}")
        self.assertEqual(case_store.DATA_DIR, original)

    def test_remote_initialization_is_idempotent_and_keeps_native_history_source(self):
        client = FakeRemote()
        first, second = remote.initialize(client), remote.initialize(client)
        self.assertEqual(first, second)
        self.assertEqual(len(client.created_items), 3)
        self.assertEqual(len(client.configs), 4)
        self.assertEqual(client.created_items[0]["source_observation_id"], "a7938ff395f644e1")
        self.assertFalse(client.scores)

    def test_remote_drift_and_unavailable_service_never_claim_success(self):
        client = FakeRemote()
        remote.initialize(client)
        client.dataset.items[0].input = {**client.dataset.items[0].input, "goal": "changed"}
        with self.assertRaisesRegex(ValueError, "not overwritten"):
            remote.initialize(client)
        self.assertEqual(len(client.created_items), 3)
        client.api.projects.get = Mock(side_effect=OSError("unavailable"))
        with self.assertRaises(OSError):
            remote.initialize(client)

    def test_incompatible_human_configs_are_not_silently_reused(self):
        client = FakeRemote()
        remote.ensure_score_configs(client)
        client.configs[0].max_value = 1
        with self.assertRaisesRegex(ValueError, "conflicts"):
            remote.ensure_score_configs(client)
        self.assertEqual(len(client.configs), 4)

    def test_human_scores_support_config_trace_comment_and_evidence_basis(self):
        client = FakeRemote()
        configs = remote.ensure_score_configs(client)
        request = CreateScoreRequest(name="evidence_support", value=1, data_type="NUMERIC", trace_id="a" * 32,
            config_id=configs["evidence_support"], comment="Finding 1 / U029:0 only supports a party statement; actual receipts missing.")
        response = client.api.scores.create(request=[request])
        self.assertEqual(response.rejected, 0)
        self.assertEqual(client.scores[0].config_id, configs["evidence_support"])
        self.assertIn("U029:0", client.scores[0].comment)

    def test_completed_and_insufficient_are_both_valid_finishes_without_semantic_score(self):
        for status in ("completed", "evidence_insufficient"):
            result = {"stop_reason": "finish_investigation", "summary_status": "completed", "completion_status": status}
            metrics, _ = scoring.automatic_metrics({"tool_calls": [], "model_calls": []}, result, 1, {"evidence_groups": []})
            self.assertEqual(metrics["runtime_finish_validated"], 1)
            self.assertNotIn("task_coverage", metrics)
            self.assertNotIn("semantic_completion", metrics)

    def test_metrics_count_errors_finish_failures_no_progress_usage_and_source_scope(self):
        audit = {"stop_reason": "no_progress_limit", "tool_calls": [
            {"name": "get_evidence", "result": {"status": "ok", "items": [{"info_unit_id": "U029", "source_ref_index": 0}]}},
            {"name": "finish_investigation", "result": {"status": "invalid_request"}},
            {"name": "get_evidence", "result": {"status": "error"}}],
            "model_calls": [{"raw_response": {"usage": {"prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 18,
                "completion_tokens_details": {"reasoning_tokens": 6}}}}]}
        criteria = {"evidence_groups": [{"id": "parties", "unit_ids": ["U029", "U036"], "mode": "all"},
                                       {"id": "one", "unit_ids": ["U029", "U036"], "mode": "any"}]}
        metrics, access = scoring.automatic_metrics(audit, {"summary_status": "failed"}, 0.5, criteria)
        self.assertEqual((metrics["tool_error_count"], metrics["finish_attempt_count"], metrics["finish_failure_count"]), (2, 1, 1))
        self.assertEqual(metrics["no_progress_limit"], 1)
        self.assertEqual(metrics["reasoning_tokens"], 6)
        self.assertEqual(metrics["output_tokens"], 8)  # reasoning already included.
        self.assertEqual(metrics["evidence_access_parties"], 0)
        self.assertEqual(metrics["evidence_access_one"], 1)
        self.assertEqual(len(access["read_source_refs"]), 1)

    def test_missing_usage_is_unknown_not_fabricated_as_zero(self):
        metrics, _ = scoring.automatic_metrics({"model_calls": [{"raw_response": None}]}, None, 1, {"evidence_groups": []}, error={"code": "llm_timeout"})
        self.assertEqual(metrics["runtime_failure"], 1)
        self.assertEqual(metrics["token_usage_complete"], 0)
        self.assertNotIn("total_tokens", metrics)
        self.assertNotIn("reasoning_tokens", metrics)

    def test_missing_audit_does_not_fabricate_execution_or_access_counts(self):
        metrics, _ = scoring.automatic_metrics({}, None, 1, {"evidence_groups": []}, error={"code": "configuration_error"})
        self.assertEqual(metrics["audit_available"], 0)
        self.assertEqual(metrics["runtime_failure"], 1)
        for name in ("model_call_count", "tool_error_count", "read_info_unit_count", "total_tokens"):
            self.assertNotIn(name, metrics)

    def test_automatic_scores_are_idempotent_and_remote_failure_is_explicit(self):
        client = FakeRemote()
        values = {"runtime_finish_validated": 1, "finish_attempt_count": 1}
        first = remote.store_scores(client, "b" * 32, values, metadata={}, comment="Execution only")
        second = remote.store_scores(client, "b" * 32, values, metadata={}, comment="Execution only")
        self.assertEqual(first, second)
        self.assertEqual(len(client.scores), 2)
        remote.verify_scores(client, "b" * 32, values)
        client.scores.clear()
        with self.assertRaisesRegex(ValueError, "not yet verified"):
            remote.verify_scores(client, "b" * 32, values)
        client.api.scores.create = Mock(return_value=NS(rejected=1))
        with self.assertRaisesRegex(ValueError, "rejected"):
            remote.store_scores(client, "b" * 32, values, metadata={}, comment="Execution only")

    def test_default_is_dry_run_and_unauthorized_live_never_loads_model_or_remote(self):
        with patch.object(runner, "dry_run", return_value={"paid_model_calls": 0}) as dry, patch.object(runner, "live") as live:
            with patch("builtins.print"):
                runner.main([])
            dry.assert_called_once()
            live.assert_not_called()
        with patch.object(llm_client, "load_config") as model:
            with self.assertRaisesRegex(ValueError, "allow-paid-models"):
                runner.live()
            with self.assertRaises(ValueError):
                runner.make_task(Mock())
            model.assert_not_called()

    def test_native_v5_offline_experiment_runs_full_agent_in_three_distinct_traces(self):
        report = runner.dry_run()
        self.assertEqual(report["paid_model_calls"], 0)
        self.assertFalse(report["remote_synchronized"])
        self.assertFalse(report["semantic_quality_scored"])
        self.assertEqual(len(set(report["trace_ids"])), 3)
        self.assertEqual(len({i["initial_case_data_sha256"] for i in report["items"]}), 1)
        self.assertEqual(report["maximum_live_model_calls"], 84)
        self.assertTrue(all(i["metrics"]["model_call_count"] == 2 for i in report["items"]))

    def test_single_task_dry_run_invokes_full_agent_exactly_once(self):
        with patch.object(runner.agent, "run_investigation", wraps=runner.agent.run_investigation) as run, \
                patch.object(runner, "_mock_reply", wraps=runner._mock_reply) as model:
            report = runner.dry_run(task_id="A1")
        self.assertEqual([item["task_id"] for item in report["items"]], ["A1"])
        self.assertEqual(len(report["trace_ids"]), 1)
        self.assertEqual(report["maximum_live_model_calls"], 28)
        self.assertEqual(report["paid_model_calls"], 0)
        self.assertEqual(run.await_count, 1)
        self.assertEqual(model.call_count, 2)
        for call in model.call_args_list:
            self.assertNotIn("investigation_opportunities", json.dumps(call.args[1:]))

    def test_selector_and_cli_reject_unknown_tasks_and_non_run_operations(self):
        dataset = NS(items=[NS(input=row["input"]) for row in data.dataset_items()])
        runner.select_dataset_items(dataset, "A2")
        self.assertEqual([item.input["task_id"] for item in dataset.items], ["A2"])
        with self.assertRaises(ValueError):
            runner.select_dataset_items(dataset, "A1")
        with self.assertRaises(ValueError):
            runner.selected_task_ids("A4")
        for args in (["dry-run", "--task", "A4"], ["init", "--task", "A1"]):
            with patch("sys.stderr"), self.assertRaises(SystemExit) as exc:
                runner.main(args)
            self.assertEqual(exc.exception.code, 2)
        with patch.object(runner, "live", return_value={}) as live, patch("builtins.print"):
            runner.main(["live", "--task", "A1", "--allow-paid-models"])
        live.assert_called_once_with(allow_paid=True, task_id="A1")

    def test_live_selection_is_one_native_execution_and_saves_manifest_before_remote_check(self):
        client = FakeRemote()
        remote.initialize(client)
        item = client.dataset.items[0]
        item.dataset_id = client.dataset.id
        result = NS(experiment_id="one-run", experiment_url="https://example.test/one-run",
            item_results=[NS(item=item, trace_id="f" * 32, output={"metrics": {"model_call_count": 1}})])
        client.dataset.run_experiment = Mock(return_value=result)
        client.flush = Mock()
        for verification_failure in (False, True):
            with self.subTest(verification_failure=verification_failure), TemporaryDirectory() as directory:
                client.dataset.items = [NS(status="ACTIVE", **row) for row in data.dataset_items()]
                with patch.object(runner, "ROOT", Path(directory)), patch.object(runner, "run_metadata", return_value={
                    "worktree_dirty": False, "code_git_commit": "a" * 40, "prompt_version": "v5"}), patch.object(
                    runner.observability, "load_settings", return_value=NS(enabled=True, capture_content=True)), patch.object(
                    runner.observability, "_get_backend", return_value=NS(client=client)), patch.object(llm_client, "load_config", return_value=NS(
                    model="mock", max_completion_tokens=20000)), patch.object(remote, "verify_experiment", side_effect=(
                    ValueError("not indexed") if verification_failure else None)), patch.object(remote.time, "sleep"):
                    if verification_failure:
                        with self.assertRaisesRegex(ValueError, "not indexed"):
                            runner.live(allow_paid=True, task_id="A1")
                    else:
                        self.assertTrue(runner.live(allow_paid=True, task_id="A1")["remote_verified"])
                saved = json.loads(next(Path(directory).glob("results/investigation/*/execution.json")).read_text(encoding="utf-8"))
                self.assertEqual(saved["remote_verified"], not verification_failure)
                self.assertEqual(saved["items"][0]["trace_id"], "f" * 32)
                self.assertEqual([row.input["task_id"] for row in client.dataset.items], ["A1"])
                client.dataset.run_experiment.assert_called_once()
                self.assertEqual(client.dataset.run_experiment.call_args.kwargs["max_concurrency"], 1)
                client.dataset.run_experiment.reset_mock()

    def test_agent_failure_is_retained_as_item_output_and_local_audit(self):
        import asyncio
        client = Mock()
        client.get_current_trace_id.return_value = "c" * 32
        item = data.dataset_items()[0]
        config = llm_client.LLMConfig("openai", "https://offline.invalid/v1", "mock-model", "unused")
        with patch.object(runner.observability, "_get_backend", return_value=None), patch.object(llm_client, "load_config", return_value=config), patch.object(
                llm_client, "call_tools", new=AsyncMock(side_effect=llm_client.LLMFailure("llm_timeout", "mock timeout"))):
            with TemporaryDirectory() as directory:
                output = asyncio.run(runner.make_task(client, offline=True, artifact_dir=directory)(item=item))
                self.assertTrue(list(Path(directory).glob("A1/runs/*.output.json")))
        self.assertEqual(output["runtime_error"]["code"], "llm_timeout")
        self.assertEqual(output["metrics"]["runtime_finish_validated"], 0)
        self.assertEqual(output["metrics"]["runtime_failure"], 1)
        client.update_current_span.assert_called_once()

    def test_missing_native_experiment_link_fails_verification(self):
        client = Mock()
        client.api.experiments.list_items.return_value = NS(data=[], meta=NS(cursor=None))
        item = NS(trace_id="d" * 32, item=NS(id="dataset-item", dataset_id="dataset"))
        with self.assertRaisesRegex(ValueError, "association"):
            remote.verify_experiment(client, NS(experiment_id="exp", item_results=[item]), datetime.now(timezone.utc))

    def test_native_association_and_nested_agent_nodes_are_verified(self):
        client = FakeRemote()
        trace_id = "d" * 32
        client.api.experiments = NS(list_items=Mock(return_value=NS(data=[NS(trace_id=trace_id,
            experiment_item_id="dataset-item", experiment_id="exp", experiment_dataset_id="dataset")], meta=NS(cursor=None))))
        metrics = {"model_call_count": 1, "tool_attempt_count": 1}
        remote.store_scores(client, trace_id, metrics, metadata={}, comment="Execution only")
        item = NS(trace_id=trace_id, item=NS(id="dataset-item", dataset_id="dataset"), output={"metrics": metrics})
        result = NS(experiment_id="exp", item_results=[item])
        rows = [NS(name=name) for name in ("experiment-item-run", "Investigation Agent", "Investigation Agent · model call",
            "Investigation Agent · tool call", "Investigation Agent · final working state")]
        with patch.object(remote, "read_trace", return_value=rows):
            remote.verify_experiment(client, result, datetime.now(timezone.utc))
            rows.pop()
            with self.assertRaisesRegex(ValueError, "final Agent state"):
                remote.verify_experiment(client, result, datetime.now(timezone.utc))
        client.api.experiments.list_items.return_value.data[0].experiment_item_id = "wrong-dataset-item"
        with self.assertRaisesRegex(ValueError, "association"):
            remote.verify_experiment(client, result, datetime.now(timezone.utc))

    def test_experiment_query_respects_server_limit_paginates_and_rejects_extra_items(self):
        client = Mock()
        rows = [NS(trace_id="t1", experiment_item_id="i1", experiment_id="exp", experiment_dataset_id="d"),
                NS(trace_id="t2", experiment_item_id="i2", experiment_id="exp", experiment_dataset_id="d")]
        client.api.experiments.list_items.side_effect = [NS(data=rows[:1], meta=NS(cursor="next")),
                                                        NS(data=rows[1:], meta=NS(cursor=None))]
        result = NS(experiment_id="exp", item_results=[NS(trace_id="t1", item=NS(id="i1", dataset_id="d"))])
        with self.assertRaisesRegex(ValueError, "association"):
            remote.verify_experiment(client, result, datetime.now(timezone.utc))
        self.assertEqual(client.api.experiments.list_items.call_count, 2)
        for call in client.api.experiments.list_items.call_args_list:
            self.assertEqual(call.kwargs["limit"], 100)
            self.assertIn("dataset", call.kwargs["fields"].split(","))
        self.assertEqual(client.api.experiments.list_items.call_args.kwargs["cursor"], "next")

    def test_score_cursor_reads_and_visibility_wait_never_repeat_writes(self):
        client = Mock()
        client.api.scores.get_many.side_effect = [NS(data=[NS(id="one")], meta=NS(cursor="next")),
                                                 NS(data=[NS(id="two")], meta=NS(cursor=None))]
        self.assertEqual([row.id for row in remote.read_scores(client, "trace")], ["one", "two"])
        for call in client.api.scores.get_many.call_args_list:
            self.assertEqual(set(call.kwargs["fields"].split(",")), {"details", "subject"})
        check = Mock(side_effect=[ValueError("indexing"), ValueError("indexing"), True])
        with patch.object(remote.time, "sleep"):
            self.assertTrue(remote.verify_with_bounded_wait(check))
        self.assertEqual(check.call_count, 3)
        client.api.scores.create.assert_not_called()

    def test_v2_trace_reads_use_raw_io_and_complete_cursor_pagination(self):
        client = Mock()
        client.api.observations.get_many.side_effect = [NS(data=[NS(id="one")], meta=NS(cursor="next")),
                                                       NS(data=[NS(id="two")], meta=NS(cursor=None))]
        now = datetime.now(timezone.utc)
        rows = remote.read_trace(client, "trace", now, now)
        self.assertEqual([r.id for r in rows], ["one", "two"])
        for call in client.api.observations.get_many.call_args_list:
            self.assertNotIn("parse_io_as_json", call.kwargs)
        self.assertEqual(remote.io_value('{"status":"ok"}'), {"status": "ok"})


if __name__ == "__main__":
    unittest.main()
