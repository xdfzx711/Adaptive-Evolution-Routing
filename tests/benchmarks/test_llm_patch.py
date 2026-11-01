import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from aer_core.errors import EvaluationError, OperationTimeout
from experiments.benchmark import prepare_benchmark
from plugins.evaluators.llm_patch import LLMPatchConfig, LLMPatchEvaluator
from plugins.storage.artifacts import LocalArtifactStore
from tests.domain_helpers import execution
from tests.execution.helpers import MODEL, RecordingModel, context, response
from tests.integration.test_swebench_static import fixture


def verdict(verdict="resolved"):
    return {"verdict": verdict, "summary": "Static assessment PRIVATE_JUDGE_REASON"}


class LLMPatchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        path, _, self.patch = fixture(self.root)
        self.prepared = prepare_benchmark(path)
        self.task = self.prepared.execution_tasks[0]
        self.spec = self.prepared.benchmark.evaluation_spec(self.task)
        self.ctx = context(stage="evaluation", workspace_ref=str(self.root / "run"), operation_id="judge")
        self.result = execution().replace(task_id=self.task.task_id,
            output={"patch": self.patch, "patch_valid": True, "syntax_valid": True})

    def evaluator(self, model, **settings):
        return LLMPatchEvaluator(LLMPatchConfig(judge_model=MODEL, **settings), model)

    def receipt(self, value):
        ref = value.to_info().evidence_ref if isinstance(value, EvaluationError) else value.evidence_refs[0]
        return json.loads(LocalArtifactStore(Path(self.ctx.workspace_ref)).get(ref))

    def test_three_scores_use_one_call_without_workspace(self):
        for label, score in [("resolved", 1), ("partial", .5), ("unresolved", 0)]:
            model = RecordingModel(lambda *_: response(json.dumps(verdict(label))))
            with patch("plugins.workspace.local.LocalRepoWorkspace.prepare", side_effect=AssertionError("No workspace")):
                assessed = self.evaluator(model).evaluate(self.task, self.result, self.spec, self.ctx)
            self.assertEqual(assessed.score, score)
            self.assertEqual(assessed.success, score == 1)
            self.assertEqual(assessed.metrics["tests_executed"], 0)
            self.assertEqual(len(model.requests), 1)
            body = json.loads(model.requests[0][0].messages[-1].content)
            self.assertEqual(body["candidate_patch"], self.patch)
            self.assertIn("private_reference_patch", body)
            self.assertNotIn("files", body)
            self.assertEqual(self.receipt(assessed)["judgment"]["verdict"], label)
            self.assertNotIn("PRIVATE_JUDGE_REASON", self.prepared.public_feedback(assessed).model_dump_json())

    def test_optional_reference_is_not_sent(self):
        model = RecordingModel(lambda *_: response(json.dumps(verdict())))
        self.evaluator(model, use_reference_patch=False).evaluate(self.task, self.result, self.spec, self.ctx)
        self.assertNotIn("private_reference_patch", json.loads(model.requests[0][0].messages[-1].content))

    def test_bad_patch_and_syntax_do_not_call_model(self):
        model = RecordingModel(lambda *_: self.fail("No review for invalid patch"))
        for updates, reason in [({"patch_valid": False}, "invalid_patch"), ({"patch": ""}, "empty_patch"),
                                ({"syntax_valid": False}, "syntax_error")]:
            result = self.result.replace(output={**dict(self.result.output), **updates})
            self.assertEqual(self.evaluator(model).evaluate(self.task, result, self.spec, self.ctx).reason_code, reason)

    def test_format_repair_is_bounded_and_accounts_both_calls(self):
        values = iter(["{", json.dumps(verdict("partial"))])
        model = RecordingModel(lambda *_: response(next(values)))
        assessed = self.evaluator(model).evaluate(self.task, self.result, self.spec, self.ctx)
        self.assertEqual(assessed.score, .5)
        self.assertEqual(assessed.usage.model_calls, 2)
        self.assertEqual(assessed.usage.total_tokens, 10)
        self.assertEqual(len(self.receipt(assessed)["calls"]), 2)

    def test_unknown_verdict_is_not_silently_scored(self):
        model = RecordingModel(lambda *_: response(json.dumps(verdict("insufficient_evidence"))))
        with self.assertRaises(EvaluationError) as caught:
            self.evaluator(model).evaluate(self.task, self.result, self.spec, self.ctx)
        self.assertEqual(len(model.requests), 2)
        self.assertNotIn("score", self.receipt(caught.exception))

    def test_fenced_json_is_accepted(self):
        model = RecordingModel(lambda *_: response('```json\n' + json.dumps(verdict()) + '\n```'))
        self.assertTrue(self.evaluator(model).evaluate(self.task, self.result, self.spec, self.ctx).success)

    def test_provider_failure_is_not_retried_or_scored(self):
        def fail(*_):
            raise OperationTimeout("fixture timeout", stage="evaluation")
        model = RecordingModel(fail)
        with self.assertRaises(EvaluationError) as caught:
            self.evaluator(model).evaluate(self.task, self.result, self.spec, self.ctx)
        self.assertEqual(len(model.requests), 1)
        self.assertFalse(caught.exception.to_info().usage.usage_complete)
        self.assertNotIn("score", self.receipt(caught.exception))

    def test_truncated_response_is_not_scored(self):
        model = RecordingModel(lambda *_: response(json.dumps(verdict())).replace(finish_reason="length"))
        with self.assertRaises(EvaluationError):
            self.evaluator(model).evaluate(self.task, self.result, self.spec, self.ctx)
        self.assertEqual(len(model.requests), 1)

    def test_context_limit_and_output_budget(self):
        model = RecordingModel(lambda *_: response(json.dumps(verdict())))
        with self.assertRaises(EvaluationError):
            self.evaluator(model, max_context_chars=2).evaluate(self.task, self.result, self.spec, self.ctx)
        self.assertFalse(model.requests)
        self.evaluator(model, max_output_tokens=512).evaluate(self.task, self.result, self.spec, self.ctx)
        self.assertLessEqual(model.requests[0][0].max_output_tokens, 512)

    def test_version_changes_with_configuration(self):
        model = RecordingModel(lambda *_: response(json.dumps(verdict())))
        self.assertNotEqual(self.evaluator(model).scorer_version,
                            self.evaluator(model, use_reference_patch=False).scorer_version)
        self.assertTrue(self.evaluator(model).scorer_version.startswith("llm.patch.proxy.v2."))
