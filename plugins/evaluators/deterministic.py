"""Binary scoring of explicit output objects, independent of Agent and SDK."""

from fractions import Fraction
import json
from uuid import NAMESPACE_URL, uuid5

from aer_core.errors import EvaluationError
from aer_core.schemas.execution import EvaluationResult, ExecutionResult, FeedbackView
from aer_core.schemas.runtime import RunContext
from aer_core.schemas.states import ExecutionStatus
from aer_core.schemas.task import EvaluationSpec, Task
from plugins.benchmarks.toy_tasks.schemas import NumericParameters, ToyInput, output_has_format, validate_scoring


class DeterministicEvaluator:
    scorer_version = "toy.scoring.v1"
    score_policy = "binary: declared output format and answer must both pass"

    def evaluate(self, task: Task, result: ExecutionResult, spec: EvaluationSpec,
                 run_ctx: RunContext) -> EvaluationResult:
        return self.evaluate_record(task, result, spec, operation_id=run_ctx.operation_id)

    def evaluate_record(self, task: Task, result: ExecutionResult, spec: EvaluationSpec,
                        *, operation_id: str) -> EvaluationResult:
        """Standalone scoring needs only an evaluation identity, no live budget."""
        try:
            if spec.task != task.identity or result.task_id != task.task_id:
                raise ValueError("task, execution and scoring identities must match")
            if task.eval_spec_ref != f"{self.scorer_version}:{task.task_id}":
                raise ValueError("task scoring reference does not match scorer version")
            if result.status not in {ExecutionStatus.COMPLETED, ExecutionStatus.STEP_LIMIT}:
                raise ValueError("interrupted/error execution is not a normal scoreable result")
            input_spec = ToyInput.model_validate(task.model_dump(mode="json")["input"])
            parameters = validate_scoring(input_spec, spec.scoring_method, spec.private_parameters)
            format_valid = output_has_format(result.output, input_spec.output_format)
            answer_correct = False
            if format_valid:
                if isinstance(parameters, NumericParameters):
                    answer_correct = abs(Fraction(str(result.output["answer"])) - Fraction(str(parameters.expected))) <= Fraction(str(parameters.absolute_tolerance))
                else:
                    answer_correct = all(result.output[name] == value for name, value in parameters.expected.items())
            success = format_valid and answer_correct
            reason_code = "correct" if success else "invalid_output_format" if not format_valid else "incorrect_answer"
            feedback = {"correct": "Output format and answer are correct.",
                        "invalid_output_format": "Output does not match the declared fields and types.",
                        "incorrect_answer": "Answer does not match the task requirements."}[reason_code]
            identity = json.dumps([task.benchmark_id, task.task_id, result.agent_version_id,
                                   result.operation_id, operation_id, self.scorer_version])
            return EvaluationResult(evaluation_id=f"evaluation-{uuid5(NAMESPACE_URL, identity)}", task_id=task.task_id,
                agent_version_id=result.agent_version_id, execution_operation_id=result.operation_id,
                operation_id=operation_id, score=float(success), success=success,
                metrics={"format_valid": float(format_valid), "answer_correct": float(answer_correct)},
                feedback=feedback, reason_code=reason_code)
        except (ValueError, ArithmeticError) as error:
            raise EvaluationError(f"Cannot evaluate execution: {error}", stage="evaluation",
                                  operation_id=operation_id, plugin_id="deterministic_evaluator") from error

    def feedback_view(self, evaluation: EvaluationResult) -> FeedbackView:
        if evaluation.reason_code not in {"correct", "invalid_output_format", "incorrect_answer"}:
            raise EvaluationError("Evaluation lacks a supported public reason code", stage="evaluation",
                                  operation_id=evaluation.operation_id, plugin_id="deterministic_evaluator")
        return FeedbackView(score=evaluation.score, success=evaluation.success,
                            reason_code=evaluation.reason_code, public_feedback=evaluation.feedback)
