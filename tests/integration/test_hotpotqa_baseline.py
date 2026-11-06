from contextlib import ExitStack
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml

from aer_core.errors import ContractError
from experiments.batch import aggregate, run_benchmark
from experiments.configuration import load_experiment_config
from experiments.bootstrap import create_registry
from experiments.execution import prepare_execution
from experiments.optimization import ConfiguredModelFactory, IsolatedCandidateTrials
from experiments.orchestration import prepare_orchestrator
from plugins.providers.scripted import ScriptedModel
from tests.benchmarks.hotpotqa_helpers import ROOT, config_file
from tests.execution.helpers import RecordingModel, context, response


@unittest.skipUnless(importlib.util.find_spec("gepa"), "install [gepa]")
class HotpotGEPATests(unittest.TestCase):
    def test_explicit_scoring_flows_through_real_gepa_protocol_and_round_logs(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            path, rows, _ = config_file(Path(directory), tasks=1)
            self.model_patches(stack, rows)
            loaded = load_experiment_config(path, overrides=["assessment.scoring.metric=joint_em"])
            summary = run_benchmark(loaded, report_holdouts=True)
            self.assertEqual(summary["status"], "completed")
            entry = summary["tasks"][0]
            self.assertEqual(entry["final_evaluation"]["score_details"]["metric"], "joint_em")
            self.assertIn(".projection.v1.", entry["final_evaluation"]["assessment_version"])
            root = Path(summary["run_root"])
            protocol = json.loads((root / "baseline_protocol.json").read_text(encoding="utf-8"))
            self.assertEqual(protocol["scoring"]["metric"]["name"], "joint_em")
            record = json.loads((root / entry["rounds"][0]["record"]).read_text(encoding="utf-8"))
            self.assertEqual(record["evaluation_before_evolution"]["score_details"]["metric"], "joint_em")
            trials = record["generation"]["internal_optimization"]["trials"]
            self.assertTrue(trials)
            # Inner trials rebuild their own plugin graph; the projection must be
            # applied there as well, with exactly the same scorer identity.
            for trial in trials:
                feedback = trial["evaluation"]
                self.assertIsNotNone(feedback)
                self.assertEqual(feedback["assessment_version"], summary["scorer_version"])
                self.assertEqual(trial["score_details"]["metric"], "joint_em")
            self.assertEqual(summary["aggregate"]["official_native_metrics_evaluated"]["joint_em"], 1)

    def three_round_config(self, path):
        return load_experiment_config(path, overrides=[
            "limits.max_evolution_rounds=3", "evolution.stop_on_success=true",
            "validation.min_improvement=0.04", "validation.max_regression_drop=0.10"])

    def test_three_round_budget_stops_on_first_accepted_success(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            path, rows, _ = config_file(Path(directory), tasks=1)
            self.model_patches(stack, rows)
            result = run_benchmark(self.three_round_config(path))
            entry = result["tasks"][0]
            self.assertEqual(len(entry["rounds"]), 1)
            self.assertEqual(entry["stop_reason"], "task_succeeded")
            self.assertTrue(entry["final_evaluation"]["success"])
            self.assertTrue(entry["rounds"][0]["candidates"][0]["committed"])

    def test_three_rejected_gepa_rounds_keep_initial_stable_version(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            path, rows, manifest = config_file(Path(directory), tasks=1)
            reflection = self.model_patches(stack, rows, regression_ids=next(iter(manifest.validation_sets.values())).regression)
            attempts = iter(range(1, 4))
            reflection.make_response = lambda *_: response(f"```\nIMPROVED reusable instructions {next(attempts)}\n```")
            result = run_benchmark(self.three_round_config(path), report_holdouts=True)
            entry = result["tasks"][0]
            self.assertEqual(len(entry["rounds"]), 3)
            self.assertEqual(entry["stop_reason"], "max_evolution_rounds_reached")
            self.assertEqual(entry["final_stable_version_id"], entry["initial_version_id"])
            for row in entry["rounds"]:
                self.assertEqual(row["candidates"][0]["acceptance"]["reason"], "regression_guard")
                self.assertFalse(row["candidates"][0]["committed"])
            self.assertTrue(entry["holdout_reporting"]["unchanged_version"])

    def test_partial_commit_survives_later_rounds_without_improvement(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            path, rows, manifest = config_file(Path(directory), tasks=1)
            self.model_patches(stack, rows)
            generate = ScriptedModel.generate
            current_id = next(iter(manifest.validation_sets))

            def partial(model, request, run_ctx):
                result = generate(model, request, run_ctx)
                if (request.messages[0].content.startswith("IMPROVED") and
                        json.loads(request.messages[-1].content)["task_id"] == current_id):
                    payload = json.loads(result.content)
                    payload["answer"] += " extra"
                    return result.replace(content=json.dumps(payload))
                return result

            stack.enter_context(patch.object(ScriptedModel, "generate", partial))
            result = run_benchmark(self.three_round_config(path), report_holdouts=True)
            entry = result["tasks"][0]
            self.assertEqual(len(entry["rounds"]), 3)
            self.assertFalse(entry["final_evaluation"]["success"])
            self.assertGreater(entry["final_evaluation"]["score"], 0)
            self.assertTrue(entry["rounds"][0]["candidates"][0]["committed"])
            self.assertEqual([row["candidates"] for row in entry["rounds"][1:]], [[], []])
            self.assertIn("IMPROVED", entry["final_prompt"])
            self.assertNotEqual(entry["final_stable_version_id"], entry["initial_version_id"])

    def model_patches(self, stack, rows, *, regression_ids=()):
        by_id = {row["_id"]: row for row in rows}
        reflection = RecordingModel(lambda *_: response("```\nIMPROVED reusable instructions\n```"))
        stack.enter_context(patch.object(ConfiguredModelFactory, "create", return_value=reflection))

        def generate(model, request, run_ctx):
            public = json.loads(request.messages[-1].content)
            row = by_id[public["task_id"]]
            improved = request.messages[0].content.startswith("IMPROVED")
            correct = not improved if row["_id"] in regression_ids else improved
            return response(json.dumps({"answer": row["answer"] if correct else "wrong",
                "supporting_facts": row["supporting_facts"] if correct else []}))

        stack.enter_context(patch.object(ScriptedModel, "generate", generate))
        return reflection

    def test_real_gepa_native_scores_commit_and_independent_batch_reset(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            path, rows, manifest = config_file(root)
            self.model_patches(stack, rows)
            summary = run_benchmark(path, run_id="batch")
            self.assertEqual(summary["status"], "completed")
            self.assertEqual(len(summary["tasks"]), 2)
            self.assertEqual(summary["aggregate"]["mean_native_metrics_evaluated"]["joint_f1"], 1)
            self.assertEqual(summary["aggregate"]["mean_score_gain_paired"], 1)
            predictions = json.loads(Path(summary["predictions_path"]).read_text(encoding="utf-8"))
            self.assertEqual(set(predictions["answer"]), {entry["task_id"] for entry in summary["tasks"]})
            for entry in summary["tasks"]:
                self.assertEqual(entry["initial_prompt"], "BAD initial prompt")
                self.assertEqual(entry["initial_evaluation"]["score"], 0)
                self.assertEqual(entry["holdouts"], manifest.validation_sets[entry["task_id"]].model_dump())
                self.assertIn("IMPROVED", entry["final_prompt"])
                self.assertNotEqual(entry["initial_version_id"], entry["final_stable_version_id"])
                round_record = entry["rounds"][0]
                self.assertEqual(round_record["internal_trials"], 6)
                acceptance = round_record["candidates"][0]["acceptance"]
                self.assertEqual(acceptance["reward"], 1)
                self.assertTrue(round_record["candidates"][0]["committed"])
                record = json.loads((Path(summary["run_root"]) / round_record["record"]).read_text(encoding="utf-8"))
                trials = record["generation"]["internal_optimization"]["trials"]
                self.assertEqual({item["task"]["task_id"] for item in trials}, {entry["task_id"]})
                self.assertEqual([item["evaluation"]["score"] for item in trials], [0, 0, 0, 1, 1, 1])
            with self.assertRaisesRegex(ValueError, "new run_id"):
                run_benchmark(path, run_id="batch")

    def test_regression_guard_rejects_and_preserves_prompt(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            path, rows, manifest = config_file(root, tasks=1)
            regression = next(iter(manifest.validation_sets.values())).regression
            self.model_patches(stack, rows, regression_ids=regression)
            summary = run_benchmark(path)
            entry = summary["tasks"][0]
            self.assertEqual(entry["initial_version_id"], entry["final_stable_version_id"])
            self.assertEqual(entry["initial_prompt"], entry["final_prompt"])
            acceptance = entry["rounds"][0]["candidates"][0]["acceptance"]
            self.assertFalse(acceptance["accepted"])
            self.assertEqual(acceptance["reason"], "regression_guard")

    def test_trials_cannot_read_holdouts_and_share_outer_metric(self):
        self.check_trial_feedback()

    def test_projected_trials_share_outer_public_rules_and_metrics(self):
        self.check_trial_feedback(projected=True)

    def check_trial_feedback(self, projected=False):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            path, rows, manifest = config_file(root, tasks=1)
            self.model_patches(stack, rows)
            prepared = prepare_execution(load_experiment_config(path, overrides=["assessment.scoring.metric=joint_f1"])
                                         if projected else path)
            current = prepared.benchmark.execution_tasks[0]
            service = IsolatedCandidateTrials(prepared.benchmark.loaded_config, create_registry(),
                prepared.storage.artifact_store, prepared.benchmark.execution_tasks)
            stable = prepared.storage.version_store.stable()
            trial = service.evaluate(current.identity, current.identity, stable, "BAD initial prompt", context())
            self.assertIsNone(trial.error)
            self.assertEqual(trial.feedback.score, 0)
            outer = prepared.run_task(current.task_id)
            self.assertEqual(trial.feedback.metrics, outer.feedback.metrics)
            self.assertEqual(trial.feedback.scoring_rules, outer.feedback.scoring_rules)
            self.assertEqual(trial.feedback.findings, outer.feedback.findings)
            self.assertEqual(trial.feedback.feedback_version, 'aer.feedback.v2')
            self.assertNotIn("PRIVATE_ANSWER", trial.model_dump_json())
            for task_id in manifest.validation_sets[current.task_id].similar + manifest.validation_sets[current.task_id].regression:
                with self.assertRaises(ContractError):
                    service.tasks(current.identity, (task_id,))

    def test_error_denominator_is_explicit(self):
        result = aggregate([{"status": "completed", "final_evaluation": {"score": 1, "success": True, "metrics": {"f1": 1}}},
                            {"status": "error", "final_evaluation": None}])
        self.assertEqual(result["evaluated_tasks"], 1)
        self.assertEqual(result["error_tasks"], 1)
        self.assertEqual(result["mean_score_evaluated"], 1)

    def test_all_episode_reporting_reuses_accepted_validation_results(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            path, rows, manifest = config_file(Path(directory), tasks=1)
            self.model_patches(stack, rows)
            summary = run_benchmark(path, report_holdouts=True)
            entry = summary["tasks"][0]
            report = entry["holdout_reporting"]
            self.assertEqual(report["status"], "complete")
            self.assertEqual(report["usage"]["total_tokens"], 0)
            self.assertTrue(all(row[name]["cache_hit"] for row in report["observations"] for name in ("initial", "final")))
            groups = summary["aggregate"]["holdouts_initial_vs_final"]
            for group in groups.values():
                self.assertEqual(group["paired_count"], 2)
                self.assertEqual(group["initial_success_rate_all"], 0)
                self.assertEqual(group["final_success_rate_all"], 1)

    def test_rejected_candidate_reporting_uses_final_stable_not_candidate(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            path, rows, manifest = config_file(Path(directory), tasks=1)
            self.model_patches(stack, rows, regression_ids=next(iter(manifest.validation_sets.values())).regression)
            summary = run_benchmark(path, report_holdouts=True)
            report = summary["tasks"][0]["holdout_reporting"]
            self.assertTrue(report["unchanged_version"])
            self.assertEqual(report["usage"]["total_tokens"], 0)
            regression = summary["aggregate"]["holdouts_initial_vs_final"]["regression"]
            self.assertEqual(regression["initial_success_rate_all"], 1)
            self.assertEqual(regression["final_success_rate_all"], 1)
            self.assertEqual(regression["transitions"]["success_to_failure"], 0)

    def test_no_candidate_still_measures_holdouts_and_preserves_version(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            path, rows, manifest = config_file(Path(directory), tasks=1)
            self.model_patches(stack, rows)
            by_id = {row["_id"]: row for row in rows}
            def perfect(model, request, run_ctx):
                row = by_id[json.loads(request.messages[-1].content)["task_id"]]
                return response(json.dumps({"answer": row["answer"], "supporting_facts": row["supporting_facts"]}))
            stack.enter_context(patch.object(ScriptedModel, "generate", perfect))
            summary = run_benchmark(path, report_holdouts=True)
            entry = summary["tasks"][0]
            self.assertEqual(entry["rounds"][0]["candidates"], [])
            self.assertEqual(entry["initial_version_id"], entry["final_stable_version_id"])
            report = entry["holdout_reporting"]
            self.assertEqual(len(report["observations"]), 4)
            self.assertTrue(all(not row["initial"]["cache_hit"] and row["final"]["cache_hit"] for row in report["observations"]))
            self.assertGreater(report["usage"]["total_tokens"], 0)
            self.assertEqual(summary["aggregate"]["current_task_initial_vs_final"]["transitions"]["both_success"], 1)

    def test_missing_holdout_observations_are_not_silently_excluded(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            path, rows, manifest = config_file(Path(directory), tasks=1)
            self.model_patches(stack, rows)
            by_id = {row["_id"]: row for row in rows}
            def fail_holdouts(model, request, run_ctx):
                row = by_id[json.loads(request.messages[-1].content)["task_id"]]
                if row["_id"] not in manifest.validation_sets:
                    raise RuntimeError("holdout execution unavailable")
                return response(json.dumps({"answer": row["answer"], "supporting_facts": row["supporting_facts"]}))
            stack.enter_context(patch.object(ScriptedModel, "generate", fail_holdouts))
            summary = run_benchmark(path, report_holdouts=True)
            self.assertEqual(summary["status"], "completed_with_errors")
            self.assertTrue(summary["tasks"][0]["final_evaluation"]["success"])
            regression = summary["aggregate"]["holdouts_initial_vs_final"]["regression"]
            self.assertEqual(regression["missing_final_count"], 2)
            self.assertEqual(regression["final_success_rate_all"], 0)
            self.assertIsNone(regression["final_mean_score_evaluated"])


class GenericBatchTests(unittest.TestCase):
    def test_existing_toy_benchmark_works_without_native_protocols(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = yaml.safe_load((ROOT / "experiments/configs/v0_1_no_evolution.yaml").read_text(encoding="utf-8"))
            data["storage"]["root"] = str(root / "runs")
            path = root / "config.yaml"
            path.write_text(yaml.safe_dump(data), encoding="utf-8")
            result = run_benchmark(path, limit=2)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["aggregate"]["evaluated_tasks"], 2)
            self.assertNotIn("predictions_path", result)

    def test_cli_checks_output_before_execution(self):
        from experiments.cli import main
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, _, _ = config_file(root)
            before = path.read_bytes()
            with patch("experiments.batch.run_benchmark") as run:
                self.assertEqual(main(["run-benchmark", str(path), "--output", str(path)]), 2)
                run.assert_not_called()
            self.assertEqual(before, path.read_bytes())
