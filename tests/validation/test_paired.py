import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch

from aer_core.errors import EvaluationError, ValidationError
from aer_core.schemas.paired import ScoredExecution, ValidationEvidence
from aer_core.usage import sum_usage
from plugins.validation.acceptance import ImprovementWithRegressionGuard
from plugins.validation.cache import LocalValidationCache
from plugins.validation.paired import PairedValidation, paired_seed
from tests.execution.helpers import context
from tests.validation.helpers import ScoreExecutor, fixture, settings


class PairedTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.artifacts, self.versions, self.stable, self.candidate, self.plan = fixture(self.directory.name)
        self.executor = ScoreExecutor()
        self.conditions = {"model": "fixture", "scoring_version": "1"}
        self.harness = PairedValidation(self.executor, self.artifacts, self.versions,
            LocalValidationCache(self.directory.name), self.conditions)

    def validate(self, candidates=None, harness=None, operation="validate-1"):
        return (harness or self.harness).validate(self.stable, candidates or (self.candidate,), self.plan,
            context(operation_id=operation, stage="local_validation"))

    def test_complete_pairing_metrics_evidence_and_stable_unchanged(self):
        report = self.validate()[0]
        self.assertEqual(report.status, "complete")
        self.assertAlmostEqual(report.baseline_metrics.score, 2 / 3)
        self.assertEqual(report.candidate_metrics.score, 1)
        self.assertEqual(report.candidate_validation_tokens, 8)
        self.assertEqual(report.usage.total_tokens, 16)
        evidence = ValidationEvidence.model_validate_json(self.artifacts.get(report.private_results_ref))
        self.assertEqual(len(evidence.results), 3)
        self.assertEqual({pair.group for pair in evidence.results}, {"similar", "regression"})
        for pair in evidence.results:
            self.assertEqual(pair.baseline.seed, pair.candidate.seed)
            self.assertEqual(pair.baseline.task, pair.candidate.task)
            self.assertFalse(pair.baseline_cached)
        self.assertEqual(self.versions.stable(), self.stable)
        self.assertTrue(ImprovementWithRegressionGuard(settings()).evaluate(report).accepted)

    def test_non_prompt_candidate_materializes_and_validates_without_prompt_replacement(self):
        with tempfile.TemporaryDirectory() as root:
            artifacts, versions, stable, candidate, plan = fixture(root, kind="memory")
            snapshot = versions.load(candidate.candidate_version_id)
            self.assertEqual(snapshot.artifacts["prompt"], stable.artifacts["prompt"])
            self.assertEqual(snapshot.artifacts["memory"], candidate.artifact_updates["memory"])
            self.assertEqual(snapshot.config, stable.config)
            harness = PairedValidation(ScoreExecutor(), artifacts, versions, LocalValidationCache(root), {})
            report = harness.validate(stable, (candidate,), plan, context(stage="local_validation"))[0]
            self.assertEqual(report.status, "complete")
            self.assertEqual(versions.stable(), stable)

    def test_local_only_improvement_does_not_change_reward(self):
        self.executor.make_score = lambda task, snapshot: float(task.task_id == "local" and snapshot.parent_version_id is not None)
        report = self.validate()[0]
        local = ValidationEvidence.model_validate_json(self.artifacts.get(report.local_result_ref)).results[0]
        self.assertTrue(local.candidate.evaluation.success)
        result = ImprovementWithRegressionGuard(settings()).evaluate(report)
        self.assertFalse(result.accepted)
        self.assertEqual(result.reward, 0)
        self.assertEqual(result.reason, "no_improvement")

    def test_repaired_local_not_required_for_acceptance(self):
        self.executor.make_score = lambda task, snapshot: float(task.task_id.startswith("reg") or
            task.task_id.startswith("similar") and snapshot.parent_version_id is not None)
        report = self.validate()[0]
        local = ValidationEvidence.model_validate_json(self.artifacts.get(report.local_result_ref)).results[0]
        self.assertFalse(local.candidate.evaluation.success)
        self.assertTrue(ImprovementWithRegressionGuard(settings()).evaluate(report).accepted)

    def test_multiple_candidates_charge_baseline_once_and_cache_reuse_costs(self):
        second = self.candidate.replace(candidate_id="c2", candidate_version_id=None)
        second = second.replace(candidate_version_id=self.versions.materialize(self.stable, second).version_id)
        reports = self.validate((self.candidate, second))
        self.assertEqual(len(self.executor.calls), 12)
        self.assertEqual(sum_usage(report.usage for report in reports).total_tokens, 24)
        self.assertEqual([report.candidate_validation_tokens for report in reports], [8, 8])
        report = self.validate(operation="validate-2")[0]
        self.assertEqual(len(self.executor.calls), 16)
        self.assertEqual(report.usage.total_tokens, 8)
        evidence = ValidationEvidence.model_validate_json(self.artifacts.get(report.private_results_ref))
        self.assertTrue(all(pair.baseline_cached for pair in evidence.results))

    def test_cache_conditions_version_and_seed_changes_prevent_reuse(self):
        self.validate()
        other = PairedValidation(self.executor, self.artifacts, self.versions, LocalValidationCache(self.directory.name),
                                 {**self.conditions, "scoring_version": "2"})
        self.validate(harness=other, operation="v2")
        self.assertEqual(len(self.executor.calls), 16)
        old_plan = self.plan
        self.plan = self.plan.replace(pair_seeds=[seed.replace(seed=seed.seed + 1) for seed in self.plan.pair_seeds])
        self.validate(operation="seed-change")
        self.assertEqual(len(self.executor.calls), 23)
        self.plan = old_plan
        stable2 = self.stable.replace(version_id="v2")
        self.versions.save(stable2)
        candidate = self.candidate.replace(candidate_id="v2-candidate", parent_version_id="v2", candidate_version_id=None)
        candidate = candidate.replace(candidate_version_id=self.versions.materialize(stable2, candidate).version_id)
        with patch.object(self.versions, "stable", return_value=stable2):
            self.harness.validate(stable2, (candidate,), self.plan, context(operation_id="version-change", stage="local_validation"))
        self.assertEqual(len(self.executor.calls), 31)

    def test_unknown_usage_is_incomplete_and_never_rewarded(self):
        self.executor.unknown = True
        report = self.validate()[0]
        self.assertEqual(report.status, "incomplete")
        self.assertIsNone(report.candidate_validation_tokens)
        self.assertIsNone(report.candidate_metrics)
        result = ImprovementWithRegressionGuard(settings()).evaluate(report)
        self.assertFalse(result.accepted)
        self.assertIsNone(result.reward)

    def test_scoring_failure_records_error_and_preserves_known_usage(self):
        execute = self.executor.execute
        def fail(task, snapshot, ctx):
            result = execute(task, snapshot, ctx)
            if task.task_id == "reg1" and snapshot.parent_version_id:
                return result.replace(evaluation=None, error=EvaluationError("fixture failure", stage="evaluation").to_info())
            return result
        self.executor.execute = fail
        report = self.validate()[0]
        self.assertEqual(report.status, "error")
        self.assertEqual(report.usage.total_tokens, 16)
        self.assertIsNone(ImprovementWithRegressionGuard(settings()).evaluate(report).reward)
        evidence = ValidationEvidence.model_validate_json(self.artifacts.get(report.private_results_ref))
        self.assertIsNone(evidence.results[1].candidate.evaluation)

    def test_pair_seed_does_not_depend_on_candidate_and_snapshot_checks(self):
        seed = paired_seed(42, self.plan.current_task.identity, self.plan.similar_tasks[0].identity)
        self.assertEqual(seed, paired_seed(42, self.plan.current_task.identity, self.plan.similar_tasks[0].identity))
        self.assertNotEqual(seed, paired_seed(43, self.plan.current_task.identity, self.plan.similar_tasks[0].identity))
        with self.assertRaises(ValidationError):
            self.validate((self.candidate.replace(candidate_version_id=None),))
        with self.assertRaises(ValidationError):
            self.validate((self.candidate, self.candidate))
        wrong = self.versions.load(self.candidate.candidate_version_id).replace(version_id="wrong", config={"different": True})
        self.versions.save(wrong)
        with self.assertRaises(ValidationError):
            self.validate((self.candidate.replace(candidate_version_id="wrong"),))

    def test_thrown_execution_and_wrong_seed_produce_error_reports(self):
        execute = self.executor.execute
        def wrong(task, snapshot, ctx):
            result = execute(task, snapshot, ctx)
            return result.replace(seed=ctx.seed + 1) if snapshot.parent_version_id else result
        self.executor.execute = wrong
        report = self.validate()[0]
        self.assertEqual(report.status, "error")
        self.assertEqual(report.usage.total_tokens, 16)
        def thrown(task, snapshot, ctx):
            if snapshot.parent_version_id:
                raise ValidationError("fixture failure", stage=ctx.stage)
            return execute(task, snapshot, ctx)
        self.executor.execute = thrown
        report = self.validate(operation="thrown")[0]
        self.assertEqual(report.status, "error")
        self.assertIsNone(report.usage.total_tokens)
        self.assertIsNone(ImprovementWithRegressionGuard(settings()).evaluate(report).reward)

    def test_incomplete_baseline_is_not_cached(self):
        execute = self.executor.execute
        def failed(task, snapshot, ctx):
            result = execute(task, snapshot, ctx)
            if not snapshot.parent_version_id and task.task_id == "reg1":
                return result.replace(evaluation=None, error=EvaluationError("fixture failure", stage="evaluation").to_info())
            return result
        self.executor.execute = failed
        self.assertEqual(self.validate()[0].status, "error")
        self.executor.execute = execute
        report = self.validate(operation="retry")[0]
        self.assertEqual(report.status, "complete")
        # Three complete baseline tasks reused; failed baseline task rerun.
        self.assertEqual(report.usage.total_tokens, 10)

    def test_regression_failure_count_tracks_old_success_lost(self):
        self.executor.make_score = lambda task, snapshot: float(
            not snapshot.parent_version_id and task.task_id.startswith("reg") or
            snapshot.parent_version_id is not None and task.task_id != "reg1")
        report = self.validate()[0]
        self.assertEqual(report.baseline_metrics.regression_failure_count, 0)
        self.assertEqual(report.candidate_metrics.regression_failure_count, 1)
