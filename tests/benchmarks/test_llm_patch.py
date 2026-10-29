import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from aer_core.errors import EvaluationError, ExecutionError, OperationTimeout
from experiments.benchmark import prepare_benchmark
from plugins.evaluators.llm_patch import LLMPatchConfig, LLMPatchEvaluator
from plugins.evaluators.static_patch import StaticPatchEvaluator, StaticPatchSettings
from plugins.storage.artifacts import LocalArtifactStore
from plugins.workspace.local import LocalRepoSettings, LocalRepoWorkspace
from tests.domain_helpers import execution
from tests.execution.helpers import MODEL, RecordingModel, context, response
from tests.integration.test_swebench_static import fixture


def verdict(verdict="resolved", snippet="answer = 2", **updates):
    return {"verdict": verdict, "summary": "Behavior reviewed PRIVATE_JUDGE_REASON",
        "evidence": [{"path": "module.py", "side": "after", "snippet": snippet}],
        "regression_findings": [], **updates}


class LLMPatchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        path, self.source, self.patch = fixture(self.root)
        self.prepared = prepare_benchmark(path)
        self.task = self.prepared.execution_tasks[0]
        self.spec = self.prepared.benchmark.evaluation_spec(self.task)
        self.workspace = LocalRepoWorkspace(LocalRepoSettings(cache_root=str(self.root / "cache"),
            repositories={"fixture/repo": str(self.source)}), self.root)
        self.ctx = context(stage="evaluation", workspace_ref=str(self.root / "run"), operation_id="judge")
        self.result = execution().replace(task_id=self.task.task_id, output={"patch": self.patch,
            "patch_valid": True, "syntax_valid": True, "base_commit": self.task.input["base_commit"]})

    def evaluator(self, model, **settings):
        return LLMPatchEvaluator(LLMPatchConfig(judge_model=MODEL, **settings), model, self.workspace)

    def test_equivalent_patch_is_reviewed_with_original_and_changed_source(self):
        alternative = self.patch.replace("+answer = 2", "+answer = 1 + 1")
        result = self.result.replace(output={**dict(self.result.output), "patch": alternative},
            state_summary={"workspace": str(self.root / "untrusted-agent-workspace")})
        def judge(query, ctx):
            body = json.loads(query.messages[-1].content)
            self.assertEqual(body["files"], [{"path": "module.py", "before": "answer = 1\n", "after": "answer = 1 + 1\n"}])
            self.assertIn("+answer = 2", body["private_reference_patch"])
            self.assertIn("+answer = 1 + 1", body["candidate_patch"])
            self.assertNotIn("untrusted-agent-workspace", json.dumps(body))
            self.assertNotIn("agent_version_id", body)
            return response(json.dumps(verdict(snippet="answer = 1 + 1")))
        model = RecordingModel(judge)
        assessed = self.evaluator(model).evaluate(self.task, result, self.spec, self.ctx)
        static = StaticPatchEvaluator(StaticPatchSettings()).evaluate(self.task, result, self.spec, self.ctx)
        self.assertLess(static.score, assessed.score)
        self.assertEqual(assessed.score, 1)
        self.assertTrue(assessed.success)
        self.assertEqual(assessed.metrics["tests_executed"], 0)
        self.assertEqual(assessed.usage.total_tokens, 5)
        self.assertEqual(assessed.usage.model_calls, 1)
        receipt = json.loads(LocalArtifactStore(Path(self.ctx.workspace_ref)).get(assessed.evidence_refs[0]))
        self.assertEqual(receipt["judgment"]["verdict"], "resolved")
        self.assertIn("PRIVATE_JUDGE_REASON", receipt["judgment"]["summary"])
        public = self.prepared.public_feedback(assessed).model_dump_json()
        self.assertNotIn("PRIVATE_JUDGE_REASON", public)
        self.assertNotIn("private_reference_patch", public)

    def test_categorical_score_and_success_are_mapped_by_code(self):
        for label, score in (("resolved", 1.0), ("partial", 0.5), ("unresolved", 0.0)):
            with self.subTest(label=label):
                model = RecordingModel(lambda *_: response(json.dumps(verdict(label))))
                result = self.evaluator(model).evaluate(self.task, self.result, self.spec, self.ctx)
                self.assertEqual(result.score, score)
                self.assertEqual(result.success, label == "resolved")

    def test_basic_invalid_empty_and_syntax_errors_do_not_call_judge(self):
        model = RecordingModel(lambda *_: self.fail("No model call for invalid artifacts"))
        for updates, reason in (({"patch_valid": False}, "invalid_patch"), ({"patch": ""}, "empty_patch"),
                                ({"syntax_valid": False}, "syntax_error")):
            with self.subTest(reason=reason), patch.object(self.workspace, "prepare", side_effect=AssertionError("No source export")):
                result = self.result.replace(output={**dict(self.result.output), **updates})
                graded = self.evaluator(model).evaluate(self.task, result, self.spec, self.ctx)
                self.assertEqual(graded.score, 0)
                self.assertEqual(graded.reason_code, reason)
                self.assertEqual(graded.usage.model_calls, 0)

    def test_failed_or_inconsistent_reviews_raise_with_usage_and_private_receipt(self):
        cases = [verdict("insufficient_evidence", evidence=[]), verdict(snippet="invented source"),
            verdict(regression_findings=["Introduced a blocking regression"]), {"verdict": "resolved"}]
        for answer in cases:
            with self.subTest(answer=answer):
                model = RecordingModel(lambda *_: response(json.dumps(answer)))
                with self.assertRaises(EvaluationError) as caught:
                    self.evaluator(model).evaluate(self.task, self.result, self.spec, self.ctx)
                self.assertEqual(caught.exception.to_info().usage.total_tokens, 5)
                self.assertIsNotNone(caught.exception.to_info().evidence_ref)
                self.assertNotIn("PRIVATE_JUDGE_REASON", str(caught.exception))

    def test_provider_failure_preserves_known_and_unknown_costs(self):
        for known in (True, False):
            def failed(*_):
                if known:
                    from aer_core.schemas.usage import ResourceUsage
                    raise OperationTimeout("provider detail", stage="evaluation", usage=ResourceUsage.zero().replace(
                        input_tokens=3, output_tokens=2, total_tokens=5, model_calls=1, usage_sources=["simulated"]))
                raise RuntimeError("unreported provider failure")
            with self.subTest(known=known), self.assertRaises(EvaluationError) as caught:
                self.evaluator(RecordingModel(failed)).evaluate(self.task, self.result, self.spec, self.ctx)
            usage = caught.exception.to_info().usage
            self.assertEqual(usage.model_calls, 1)
            self.assertEqual(usage.total_tokens, 5 if known else None)
            self.assertEqual(usage.usage_complete, known)

    def test_nonapplicable_patch_scores_zero_but_git_timeout_is_a_scoring_error(self):
        model = RecordingModel(lambda *_: self.fail("No model call before a valid source export"))
        changed = self.result.replace(output={**dict(self.result.output),
            "patch": self.patch.replace("-answer = 1", "-answer = 9")})
        assessed = self.evaluator(model).evaluate(self.task, changed, self.spec, self.ctx)
        self.assertEqual(assessed.reason_code, "invalid_patch")
        self.assertEqual(assessed.score, 0)
        def timeout(*args, **kwargs):
            raise ExecutionError("Git timed out", stage="evaluation") from subprocess.TimeoutExpired("git", 1)
        with patch("plugins.evaluators.patch_context.git", side_effect=timeout):
            with self.assertRaises(EvaluationError) as caught:
                self.evaluator(model).evaluate(self.task, self.result, self.spec, self.ctx)
        self.assertEqual(caught.exception.to_info().usage.model_calls, 0)

    def test_missing_context_is_a_scoring_error_and_reference_can_be_disabled(self):
        model = RecordingModel(lambda *_: response(json.dumps(verdict())))
        with self.assertRaises(EvaluationError):
            self.evaluator(model, max_context_chars=2).evaluate(self.task, self.result, self.spec, self.ctx)
        self.assertFalse(model.requests)
        evaluated = self.evaluator(model, use_reference_patch=False).evaluate(self.task, self.result, self.spec, self.ctx)
        self.assertTrue(evaluated.success)
        self.assertNotIn("private_reference_patch", json.loads(model.requests[-1][0].messages[-1].content))

    def test_scorer_version_tracks_resolved_model_and_rubric_configuration(self):
        model = RecordingModel(lambda *_: response(json.dumps(verdict())))
        model.model_id = "judge-a"
        first = self.evaluator(model).scorer_version
        model.model_id = "judge-b"
        self.assertNotEqual(first, self.evaluator(model).scorer_version)
        self.assertNotEqual(self.evaluator(model).scorer_version, self.evaluator(model, use_reference_patch=False).scorer_version)
