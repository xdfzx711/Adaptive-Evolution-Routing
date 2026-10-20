import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch

from aer_core.cached_executor import CachedTaskExecutor
from aer_core.errors import ContractError
from plugins.validation.cache import LocalValidationCache
from tests.execution.helpers import context
from tests.validation.helpers import ScoreExecutor, fixture


class CachedExecutorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        _, _, self.snapshot, _, self.plan = fixture(self.directory.name)
        self.executor = ScoreExecutor()
        self.cache = LocalValidationCache(self.directory.name)
        self.cached = CachedTaskExecutor(self.executor, self.cache, {"scorer_version": "1"})
        self.context = context(operation_id="initial-observation", stage="initial_execution")

    def test_zero_score_is_frozen_across_initial_candidate_and_baseline_roles(self):
        task = self.plan.current_task
        first = self.cached.execute(task, self.snapshot, self.context)
        self.assertEqual(first.evaluation.score, 0)
        self.executor.make_score = lambda task, snapshot: 1.0
        again = self.cached.execute(task, self.snapshot, replace(self.context, operation_id="next-baseline", stage="local_validation"))
        self.assertEqual(len(self.executor.calls), 1)
        self.assertEqual(first.execution, again.execution)
        self.assertEqual(first.evaluation, again.evaluation)
        self.assertEqual(first.usage, again.usage)
        self.assertEqual(again.reuse_operation_id, "next-baseline")
        self.assertEqual(again.charged_usage.total_tokens, 0)
        self.assertTrue(again.cache_hit)

    def test_seed_runtime_task_version_and_scoring_conditions_require_new_observations(self):
        task = self.plan.current_task
        self.cached.execute(task, self.snapshot, self.context)
        for ctx in (replace(self.context, seed=self.context.seed + 1),
                    replace(self.context, max_output_tokens=self.context.max_output_tokens + 1),
                    replace(self.context, model_timeout_seconds=self.context.model_timeout_seconds + 1),
                    replace(self.context, tool_timeout_seconds=self.context.tool_timeout_seconds + 1)):
            self.assertFalse(self.cached.execute(task, self.snapshot, ctx).cache_hit)
        self.cached.execute(self.plan.similar_tasks[0], self.snapshot, self.context)
        self.cached.execute(task, self.snapshot.replace(version_id="another-version"), self.context)
        CachedTaskExecutor(self.executor, self.cache, {"scorer_version": "2"}).execute(task, self.snapshot, self.context)
        self.assertEqual(len(self.executor.calls), 8)

    def test_incomplete_cost_is_not_cached_and_can_be_retried(self):
        snapshot = self.snapshot.replace(version_id="candidate", parent_version_id=self.snapshot.version_id)
        self.executor.unknown = True
        first = self.cached.execute(self.plan.current_task, snapshot, self.context)
        self.assertFalse(first.complete)
        self.executor.unknown = False
        second = self.cached.execute(self.plan.current_task, snapshot, self.context)
        self.assertFalse(second.cache_hit)
        self.assertTrue(second.complete)
        self.assertEqual(len(self.executor.calls), 2)

    def test_mismatched_executor_identity_is_not_cached_and_retains_new_cost(self):
        original = self.executor.execute
        with patch.object(self.executor, "execute", side_effect=lambda *args: original(*args).replace(seed=self.context.seed + 1)):
            with self.assertRaises(ContractError) as failure:
                self.cached.execute(self.plan.current_task, self.snapshot, self.context)
        self.assertEqual(failure.exception.to_info().usage.total_tokens, 2)
        self.assertFalse(self.cached.execute(self.plan.current_task, self.snapshot, self.context).cache_hit)
        self.assertEqual(len(self.executor.calls), 2)
