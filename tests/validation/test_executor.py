import unittest
from unittest.mock import patch

from aer_core.errors import EvaluationError, ExecutionError
from aer_core.task_executor import ScoredTaskExecutor
from plugins.benchmarks.toy_tasks import ToyBenchmark
from plugins.evaluators.deterministic import DeterministicEvaluator
from tests.execution.helpers import agent_fixture, context


class TaskExecutorTests(unittest.TestCase):
    def setUp(self):
        self.agent, self.snapshot, _ = agent_fixture(single=True)
        self.benchmark = ToyBenchmark()
        self.evaluator = DeterministicEvaluator()
        self.executor = ScoredTaskExecutor(self.agent, self.benchmark, self.evaluator)
        self.task = self.benchmark.tasks("validation")[0]

    def test_explicit_snapshot_and_validation_task_are_executed_and_scored(self):
        result = self.executor.execute(self.task, self.snapshot, context(stage="similar_validation", seed=17))
        self.assertTrue(result.complete)
        self.assertEqual(result.seed, 17)
        self.assertEqual(result.task, self.task.identity)
        self.assertTrue(result.evaluation.success)
        self.assertTrue(result.execution.state_summary["fresh_session"])
        self.assertEqual(result.version_id, self.snapshot.version_id)

    def test_scoring_failure_preserves_execution_and_usage(self):
        with patch.object(self.evaluator, "evaluate", side_effect=EvaluationError("fixture failure", stage="evaluation")):
            result = self.executor.execute(self.task, self.snapshot, context(stage="similar_validation"))
        self.assertFalse(result.complete)
        self.assertIsNotNone(result.execution)
        self.assertIsNone(result.evaluation)
        self.assertGreater(result.usage.total_tokens, 0)
        self.assertEqual(result.error.stage, "evaluation")

    def test_thrown_execution_without_usage_does_not_invent_known_zero(self):
        with patch.object(self.agent, "run", side_effect=ExecutionError("fixture failure", stage="similar_validation")):
            result = self.executor.execute(self.task, self.snapshot, context(stage="similar_validation"))
        self.assertIsNone(result.execution)
        self.assertIsNone(result.evaluation)
        self.assertIsNone(result.usage.total_tokens)
        self.assertFalse(result.complete)

    def test_execution_and_evaluation_identity_mismatches_are_errors(self):
        original = self.agent.run
        with patch.object(self.agent, "run", side_effect=lambda *args: original(*args).replace(task_id="foreign")):
            result = self.executor.execute(self.task, self.snapshot, context(stage="similar_validation"))
        self.assertFalse(result.complete)
        self.assertIsNone(result.execution)
        original_evaluate = self.evaluator.evaluate
        with patch.object(self.evaluator, "evaluate", side_effect=lambda *args: original_evaluate(*args).replace(task_id="foreign")):
            result = self.executor.execute(self.task, self.snapshot, context(stage="similar_validation"))
        self.assertFalse(result.complete)
        self.assertIsNone(result.evaluation)

    def test_model_failure_retains_reported_call_cost(self):
        from tests.execution.helpers import RecordingModel, response
        self.agent.model = RecordingModel(lambda *args: response(unknown=True))
        result = self.executor.execute(self.task, self.snapshot, context(stage="similar_validation"))
        self.assertFalse(result.complete)
        self.assertIsNone(result.usage.total_tokens)
        self.assertEqual(result.usage.model_calls, 1)
