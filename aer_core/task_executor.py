"""Fresh-session execution and external scoring of an explicit Snapshot."""

from dataclasses import replace
from time import monotonic

from aer_core.contracts.agent import AgentPlugin
from aer_core.contracts.benchmark import BenchmarkPlugin
from aer_core.contracts.evaluator import EvaluatorPlugin
from aer_core.errors import AERError, ContractError, EvaluationError, ExecutionError
from aer_core.schemas.execution import EvaluationResult, ExecutionResult
from aer_core.schemas.paired import ScoredExecution
from aer_core.schemas.references import AgentSnapshot
from aer_core.schemas.runtime import RunContext
from aer_core.schemas.task import EvaluationSpec, Task, TaskView
from aer_core.schemas.usage import ResourceUsage
from aer_core.schemas.validation import evaluations_match
from aer_core.usage import sum_usage


class ScoredTaskExecutor:
    def __init__(self, agent: AgentPlugin, benchmark: BenchmarkPlugin, evaluator: EvaluatorPlugin):
        self.agent, self.benchmark, self.evaluator = agent, benchmark, evaluator

    def execute(self, task: Task, snapshot: AgentSnapshot, run_ctx: RunContext) -> ScoredExecution:
        begin = monotonic()
        result, evaluation, error = None, None, None
        reports = []
        stage = run_ctx.stage
        try:
            public = self.benchmark.task_view(task)
            if not isinstance(public, TaskView) or (public.benchmark_id, public.task_id) != (task.benchmark_id, task.task_id):
                raise ContractError("Benchmark must return a matching public TaskView", stage=stage)
            result = self.agent.run(public, snapshot, run_ctx)
            if not isinstance(result, ExecutionResult):
                result = None
                raise ContractError("Agent must return ExecutionResult", stage=stage)
            reports.append(result.usage)
            if result.task_id != task.task_id or result.agent_version_id != snapshot.version_id or result.operation_id != run_ctx.operation_id:
                result = None
                raise ContractError("Agent execution identities differ from task/Snapshot/operation", stage=stage)
            if result.status not in {"completed", "step_limit"}:
                error = result.error or ExecutionError("Execution did not produce a scoreable result", stage=stage).to_info()
            else:
                stage = "evaluation"
                assessment = replace(run_ctx, stage=stage, operation_id=run_ctx.operation_id + "/evaluation")
                spec = self.benchmark.evaluation_spec(task)
                if not isinstance(spec, EvaluationSpec) or spec.task != task.identity:
                    raise ContractError("Scoring specification must match the selected task", stage=stage)
                evaluation = self.evaluator.evaluate(task, result, spec, assessment)
                if not isinstance(evaluation, EvaluationResult):
                    evaluation = None
                    raise ContractError("Evaluator must return EvaluationResult", stage=stage)
                if evaluation.usage is not None:
                    reports.append(evaluation.usage)
                if not evaluations_match(result, evaluation) or evaluation.operation_id != assessment.operation_id:
                    evaluation = None
                    raise ContractError("Evaluator returned mismatched execution identities", stage=stage)
        except Exception as failure:
            if isinstance(failure, AERError):
                error = failure.to_info()
            else:
                error_type = EvaluationError if stage == "evaluation" else ExecutionError
                error = error_type("Execution or external scoring failed", stage=stage, operation_id=run_ctx.operation_id).to_info()
            if error.usage is not None:
                reports.append(error.usage)
            elif stage != "evaluation" and not reports:
                reports.append(ResourceUsage(input_tokens=None, output_tokens=None, total_tokens=None, model_calls=0,
                    tool_calls=0, elapsed_seconds=0, usage_complete=False, usage_sources=["unknown"]))
            evaluation = None
        usage = sum_usage(reports).replace(elapsed_seconds=monotonic() - begin)
        return ScoredExecution(task=task.identity, version_id=snapshot.version_id, seed=run_ctx.seed,
            execution=result, evaluation=evaluation, usage=usage, error=error)
