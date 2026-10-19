import unittest
from dataclasses import replace
from decimal import localcontext

from aer_core.contracts.evaluator import EvaluatorPlugin
from aer_core.errors import EvaluationError
from aer_core.schemas.execution import EvaluationResult
from aer_core.schemas.states import RunStage
from plugins.benchmarks.toy_tasks import ToyBenchmark
from plugins.evaluators.deterministic import DeterministicEvaluator
from tests.domain_helpers import execution, run_context


class EvaluatorTests(unittest.TestCase):
    def setUp(self):
        self.benchmark = ToyBenchmark()
        self.evaluator = DeterministicEvaluator()

    def score(self, task, output, version="v1", spec=None, status="completed"):
        result = execution(version).replace(task_id=task.task_id, output=output, status=status)
        return self.evaluator.evaluate_record(task, result, spec or self.benchmark.evaluation_spec(task),
                                              operation_id="evaluation-1")

    def test_correct_answers_for_every_task_and_serializable_results(self):
        self.assertIsInstance(self.evaluator, EvaluatorPlugin)
        # Expected values are supplied from the private test side, never an Agent input.
        for split in ("dev", "validation", "test"):
            for task in self.benchmark.tasks(split):
                spec = self.benchmark.evaluation_spec(task)
                expected = spec.model_dump(mode="json")["private_parameters"]["expected"]
                output = {"answer": expected} if spec.scoring_method == "numeric_answer" else expected
                with self.subTest(task=task.task_id):
                    score = self.score(task, output)
                    self.assertEqual(score.score, 1)
                    self.assertTrue(score.success)
                    self.assertEqual(score.metrics, {"format_valid": 1, "answer_correct": 1})
                    self.assertEqual(EvaluationResult.model_validate_json(score.model_dump_json()), score)

    def test_fixture_answers_are_independently_checked(self):
        # Hand checked examples prevent simply testing the scorer against its own rubric.
        for task_id, output in {"dev-001": {"answer": 75}, "dev-002": {"answer": 7},
                               "dev-003": {"name": "Mira", "count": 6},
                               "dev-004": {"answer": "APPLE,PEAR,PLUM"},
                               "validation-001": {"answer": 84}, "validation-002": {"answer": 1.75},
                               "validation-003": {"name": "Owen", "count": 11},
                               "validation-004": {"answer": "ASH,BIRCH,CEDAR"},
                               "validation-005": {"answer": 13}, "validation-006": {"answer": 13},
                               "validation-007": {"name": "Tess", "count": 3},
                               "validation-008": {"answer": "AMBER,BLUE"},
                               "test-001": {"answer": 72}, "test-002": {"answer": 17},
                               "test-003": {"name": "Lena", "count": 8},
                               "test-004": {"answer": "FIG,GRAPE,KIWI"}}.items():
            task = next(t for split in ("dev", "validation", "test") for t in self.benchmark.tasks(split) if t.task_id == task_id)
            with self.subTest(task=task_id):
                self.assertTrue(self.score(task, output).success)

    def test_numeric_wrong_answer_and_invalid_formats_are_distinct(self):
        task = self.benchmark.tasks("dev")[0]
        wrong = self.score(task, {"answer": 74})
        self.assertEqual(wrong.reason_code, "incorrect_answer")
        self.assertEqual(wrong.metrics["format_valid"], 1)
        for output in ({}, {"answer": "75"}, {"answer": True}, {"answer": 75.0},
                       {"answer": 75, "explanation": "computed"}, {"raw_text": '{"answer":75}'}):
            with self.subTest(output=output):
                scored = self.score(task, output)
                self.assertFalse(scored.success)
                self.assertEqual(scored.reason_code, "invalid_output_format")

    def test_public_numeric_tolerance_boundary_and_decimal_context(self):
        task = self.benchmark.tasks("validation")[1]
        for value, correct in [(1.75, True), (1.750001, True), (1.749999, True),
                               (1.7500011, False), (1.7499989, False), (2, False)]:
            with self.subTest(value=value), localcontext() as ctx:
                ctx.prec = 2
                self.assertEqual(self.score(task, {"answer": value}).success, correct)

    def test_structured_type_and_value_checks_do_not_coerce(self):
        task = self.benchmark.tasks("dev")[2]
        for output, reason in [({"count": 6, "name": "Mira"}, "correct"),
                               ({"count": 6, "name": "mira"}, "incorrect_answer"),
                               ({"count": "6", "name": "Mira"}, "invalid_output_format"),
                               ({"count": True, "name": "Mira"}, "invalid_output_format"),
                               ({"count": 6}, "invalid_output_format")]:
            with self.subTest(output=output):
                self.assertEqual(self.score(task, output).reason_code, reason)

    def test_publicly_allowed_extra_fields_are_ignored(self):
        task = self.benchmark.tasks("dev")[2]
        data = task.model_dump(mode="json")["input"]
        data["output_format"]["allow_extra_fields"] = True
        changed = task.replace(input=data)
        self.assertTrue(self.score(changed, {"name": "Mira", "count": 6, "extra": "comment"},
                                   spec=self.benchmark.evaluation_spec(task)).success)

    def test_scoring_is_repeatable_and_tracks_versions_and_execution_ids(self):
        task = self.benchmark.tasks("dev")[0]
        first = self.score(task, {"answer": 75}, "baseline")
        again = self.score(task, {"answer": 75}, "baseline")
        candidate = self.score(task, {"answer": 75}, "candidate")
        self.assertEqual(first, again)
        self.assertEqual(first.score, candidate.score)
        self.assertEqual(first.agent_version_id, "baseline")
        self.assertEqual(candidate.agent_version_id, "candidate")
        self.assertEqual(candidate.execution_operation_id, "exec-candidate")
        self.assertNotEqual(first.evaluation_id, candidate.evaluation_id)

    def test_same_result_scores_through_protocol_and_standalone_entry(self):
        task = self.benchmark.tasks("dev")[0]
        result = execution().replace(output={"answer": 75})
        context = replace(run_context(), stage=RunStage.EVALUATION, operation_id="evaluation-1")
        scored = self.evaluator.evaluate(task, result, self.benchmark.evaluation_spec(task), context)
        self.assertEqual(scored, self.score(task, {"answer": 75}))

    def test_public_feedback_does_not_disclose_answer_or_spec(self):
        task = self.benchmark.tasks("dev")[0]
        scored = self.score(task, {"answer": 74})
        feedback = self.evaluator.feedback_view(scored)
        self.assertEqual(feedback.reason_code, "incorrect_answer")
        self.assertNotIn("75", feedback.model_dump_json())
        self.assertNotIn("expected", feedback.model_dump_json())
        self.assertEqual(scored.evidence_refs, ())

    def test_foreign_identity_bad_rubric_and_unsupported_method_raise_errors(self):
        task = self.benchmark.tasks("dev")[0]
        spec = self.benchmark.evaluation_spec(task)
        cases = [(task.replace(benchmark_id="other"), spec),
                 (task, spec.replace(task=spec.task.replace(task_id="other"))),
                 (task.replace(eval_spec_ref="other"), spec),
                 (task, spec.replace(scoring_method="unknown")),
                 (task, spec.replace(private_parameters={"expected": 75, "absolute_tolerance": 0.5})),
                 (task, spec.replace(private_parameters={"expected": True, "absolute_tolerance": 0}))]
        for changed, rubric in cases:
            with self.subTest(task=changed, rubric=rubric), self.assertRaises(EvaluationError):
                self.score(changed, {"answer": 75}, spec=rubric)
        with self.assertRaises(EvaluationError):
            self.evaluator.evaluate_record(task, execution().replace(task_id="foreign"), spec, operation_id="eval-1")

    def test_budget_interruption_is_not_a_zero_score(self):
        task = self.benchmark.tasks("dev")[0]
        with self.assertRaises(EvaluationError):
            self.score(task, {}, status="budget_interrupted")
        self.assertEqual(self.score(task, {}, status="step_limit").score, 0)
