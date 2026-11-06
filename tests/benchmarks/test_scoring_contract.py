"""Scoring projection, protocol and native aggregation contract tests."""

from dataclasses import replace
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from aer_core.errors import ContractError, EvaluationError
from aer_core.scoring import configure_scoring
from aer_core.schemas.scoring import MetricDefinition, ScoringSettings, ScoreTransform
from experiments.benchmark import prepare_benchmark
from experiments.configuration import load_experiment_config
from experiments.conditions import validation_conditions
from experiments.scoring import aggregate_declared_metrics, scoring_protocol
from plugins.evaluators.hotpotqa import HotpotQAEvaluator, HotpotQAEvaluatorSettings
from tests.benchmarks.hotpotqa_helpers import config_file
from tests.domain_helpers import evaluation, execution, run_context


class DeclaredEvaluator:
    scorer_version = "test.official.v1"
    score_policy = "official fixture"
    success_policy = "official pass flag"

    def __init__(self, metric, value):
        self.metric, self.value = metric, value

    def metric_definitions(self):
        return (self.metric,)

    def evaluate(self, task, result, spec, run_ctx):
        return evaluation(result, score=0, success=True).replace(metrics={self.metric.name: self.value})


class ScoringTests(unittest.TestCase):
    def test_fixed_ranges_directions_and_raw_metrics_preserve_success(self):
        for direction, value, expected in [("higher", 75, .75), ("lower", 75, .25)]:
            native = DeclaredEvaluator(MetricDefinition(name="points", direction=direction,
                minimum=0, maximum=100, kind="continuous", aggregation="mean"), value)
            scorer = configure_scoring(native, ScoringSettings(metric="points",
                transform=ScoreTransform(type="fixed_range")))
            result = scorer.evaluate(None, execution(), None, run_context())
            self.assertEqual(result.score, expected)
            self.assertTrue(result.success)
            self.assertEqual(result.metrics["points"], value)
            self.assertEqual(result.score_details.raw_score, value)
            self.assertEqual(result.assessment_version, scorer.scorer_version)

    def test_legacy_evaluator_is_unchanged_and_invalid_setup_fails(self):
        legacy = object()
        self.assertIs(configure_scoring(legacy, None), legacy)
        with self.assertRaises(ContractError):
            configure_scoring(legacy, ScoringSettings(metric="score"))
        for metric, settings in [
            (MetricDefinition(name="x"), ScoringSettings(metric="x")),
            (MetricDefinition(name="x", direction="lower", minimum=0, maximum=1), ScoringSettings(metric="x")),
            (MetricDefinition(name="x", direction="higher", minimum=0, maximum=100), ScoringSettings(metric="x")),
            (MetricDefinition(name="x", direction="higher"), ScoringSettings(metric="x", transform=ScoreTransform(type="fixed_range"))),
            (MetricDefinition(name="x", direction="higher", minimum=0, maximum=1), ScoringSettings(metric="missing")),
        ]:
            with self.subTest(metric=metric, settings=settings), self.assertRaises(ContractError):
                configure_scoring(DeclaredEvaluator(metric, 0), settings)

    def test_unknown_range_requires_explicit_fixed_bounds_no_clipping(self):
        native = DeclaredEvaluator(MetricDefinition(name="x", direction="lower"), 20)
        settings = ScoringSettings(metric="x", transform=ScoreTransform(type="fixed_range", minimum=10, maximum=30))
        scorer = configure_scoring(native, settings)
        self.assertEqual(scorer.evaluate(None, execution(), None, run_context()).score, .5)
        native.value = 31
        with self.assertRaises(EvaluationError):
            scorer.evaluate(None, execution(), None, run_context())

    def test_binary_metadata_and_missing_metric_violations_are_errors(self):
        native = DeclaredEvaluator(MetricDefinition(name="x", direction="higher", minimum=0, maximum=1, kind="binary"), .5)
        scorer = configure_scoring(native, ScoringSettings(metric="x"))
        with self.assertRaises(EvaluationError):
            scorer.evaluate(None, execution(), None, run_context())
        with patch.object(native, "evaluate", return_value=evaluation().replace(metrics={})):
            with self.assertRaises(EvaluationError):
                scorer.evaluate(None, execution(), None, run_context())

    def test_native_micro_aggregation_is_not_mean_of_task_ratios(self):
        metric = MetricDefinition(name="ratio", direction="higher", minimum=0, maximum=1,
            aggregation="ratio_of_sums", numerator="passed", denominator="total")
        native = DeclaredEvaluator(metric, 0)
        rows = [{"metrics": {"ratio": 1, "passed": 1, "total": 1}},
                {"metrics": {"ratio": .5, "passed": 1, "total": 2}}]
        self.assertEqual(aggregate_declared_metrics(rows, native)["ratio"], 2 / 3)

    def test_hotpot_legacy_fingerprint_and_explicit_profile(self):
        from hashlib import sha256
        settings = HotpotQAEvaluatorSettings()
        expected = sha256(json.dumps(settings.model_dump(), sort_keys=True).encode()).hexdigest()[:12]
        legacy = HotpotQAEvaluator(settings)
        self.assertEqual(legacy.scorer_version, "hotpotqa.official.3635853403a8.v1." + expected)
        with tempfile.TemporaryDirectory() as directory:
            path, rows, _ = config_file(Path(directory), tasks=1)
            before = prepare_benchmark(path)
            after = prepare_benchmark(load_experiment_config(path, overrides=[
                "assessment.scoring.metric=joint_f1"]))
            task = before.execution_tasks[0]
            row = next(row for row in rows if row["_id"] == task.task_id)
            output = execution().replace(task_id=task.task_id,
                output={"answer": row["answer"] + " extra", "supporting_facts": row["supporting_facts"]})
            a, b = [p.score(output, operation_id="evaluation") for p in (before, after)]
            self.assertEqual((a.score, a.success, dict(a.metrics)), (b.score, b.success, dict(b.metrics)))
            self.assertIsNone(scoring_protocol(before))
            self.assertNotEqual(before.evaluator.scorer_version, after.evaluator.scorer_version)
            self.assertEqual(after.as_dict()["scoring"]["metric"]["name"], "joint_f1")
            self.assertIsNotNone(b.score_details)

    def test_binary_protocol_does_not_change_thresholds_or_confuse_inner_sampling(self):
        with tempfile.TemporaryDirectory() as directory:
            path, _, _ = config_file(Path(directory), tasks=1)
            loaded = load_experiment_config(path, overrides=["assessment.scoring.metric=joint_em",
                "validation.min_improvement=0.04", "validation.max_regression_drop=0.10"])
            prepared = prepare_benchmark(loaded)
            # Fixture reserves two holdouts per group; use one for this lattice check.
            with patch.object(type(prepared), "validation_groups", return_value=((1,), (2,))):
                profile = scoring_protocol(prepared)
            actual = profile["threshold_interpretation"]["binary_lattice"][0]
            self.assertEqual(actual["minimum_positive_lattice_gain_meeting_threshold"], .25)
            self.assertEqual(actual["max_regression_net_success_loss"], 0)
            self.assertEqual(profile["inner_sampling"]["prompt"]["samples_per_evaluation"], 3)
            self.assertEqual(loaded.config.validation.min_improvement, .04)

    def test_explicit_projection_disallows_different_inner_metric_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            path, _, _ = config_file(Path(directory), tasks=1)
            loaded = load_experiment_config(path, overrides=["assessment.scoring.metric=joint_f1",
                "targets.operator_configs.prompt.trial_evaluator={plugin: hotpotqa_evaluator, config: {primary_metric: em}}"])
            with self.assertRaises(ContractError):
                prepare_benchmark(loaded)

    def test_scoring_protocol_changes_cache_conditions_but_threshold_does_not(self):
        from experiments.execution import prepare_execution
        with tempfile.TemporaryDirectory() as directory:
            path, _, _ = config_file(Path(directory), tasks=1)
            setup = prepare_execution(path)
            baseline = validation_conditions(setup)
            self.assertNotIn("scoring", baseline)
            changed = replace(setup, benchmark=prepare_benchmark(load_experiment_config(path,
                overrides=["assessment.scoring.metric=joint_em"])))
            conditions = validation_conditions(changed)
            self.assertNotEqual(baseline, conditions)
            changed_threshold = replace(setup, benchmark=prepare_benchmark(load_experiment_config(path,
                overrides=["assessment.scoring.metric=joint_em", "validation.min_improvement=0.2"])))
            self.assertEqual(conditions, validation_conditions(changed_threshold))


if __name__ == "__main__":
    unittest.main()
