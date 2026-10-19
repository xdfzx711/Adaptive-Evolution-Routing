"""Small domain fixtures with explicit identities; no runtime plugin logic."""

from datetime import datetime, timezone

from aer_core.schemas.evolution import EvolutionContext, EvolutionDecision
from aer_core.schemas.execution import EvaluationResult, ExecutionResult, ExecutionView, FeedbackView, TraceEvent
from aer_core.schemas.experiment import RunResult
from aer_core.schemas.references import AgentSnapshot, ArtifactRef, RecordRef
from aer_core.schemas.runtime import RunContext
from aer_core.schemas.states import RunStage
from aer_core.schemas.task import Task, TaskView
from aer_core.schemas.usage import BudgetRemaining, ResourceUsage
from aer_core.schemas.validation import AcceptanceResult, ValidationMetrics, ValidationReport


NOW = datetime(2026, 10, 13, tzinfo=timezone.utc)


def artifact(kind="prompt", version="v1"):
    return ArtifactRef(artifact_id=f"{kind}-{version}", kind=kind, version=version, uri=f"artifacts/{kind}-{version}.json", digest=f"content-{kind}-{version}")


def snapshot(version="v1", parent=None):
    return AgentSnapshot(version_id=version, agent_plugin_id="mock_agent", config={"nested": {"steps": [1, 2]}}, artifacts={"prompt": artifact(version=version)}, parent_version_id=parent)


def task(task_id="dev-001"):
    return Task(task_id=task_id, benchmark_id="toy_tasks", input={"question": "1 + 1"}, metadata={"category": "arithmetic"}, eval_spec_ref="private-eval.json")


def task_view():
    return TaskView(task_id="dev-001", benchmark_id="toy_tasks", input={"question": "1 + 1"}, public_metadata={"category": "arithmetic"})


def execution(version="v1"):
    return ExecutionResult(task_id="dev-001", agent_version_id=version, operation_id=f"exec-{version}", output={"answer": "2"}, trajectory=[TraceEvent(seq=1, kind="finish", payload={"answer": "2"}, timestamp=NOW)], state_summary={}, usage=ResourceUsage.zero(), status="completed")


def evaluation(result=None, score=0.0, success=False):
    result = result or execution()
    return EvaluationResult(evaluation_id=f"eval-{result.operation_id}", task_id=result.task_id, agent_version_id=result.agent_version_id, execution_operation_id=result.operation_id, operation_id=f"score-{result.operation_id}", score=score, success=success, metrics={"score": score}, feedback="public feedback")


def context():
    result = execution()
    return EvolutionContext(task=task_view(), stable_snapshot=snapshot(), execution=ExecutionView(task_id=result.task_id, agent_version_id=result.agent_version_id, output=result.model_dump()["output"], trajectory=result.trajectory, state_summary={}, usage=result.usage), feedback=FeedbackView(score=0, success=False, reason_code="invalid_output", public_feedback="use the expected format", evidence_step_ids=[1]), diagnosis=None, history=[], feasible_targets=["prompt"], remaining_budget=remaining(), as_of=NOW)


def remaining():
    return BudgetRemaining(tokens=100, tool_calls=10, elapsed_seconds=60)


class StubBudget:
    def remaining(self):
        return remaining()

    def reserve(self, operation_id, stage, *, tokens, tool_calls):
        raise NotImplementedError("budget implementation belongs to step 6")

    def settle(self, operation_id, usage):
        raise NotImplementedError("budget implementation belongs to step 6")


def run_context():
    return RunContext(run_id="run-1", episode_id="episode-1", operation_id="evolve-1", stage=RunStage.EVOLUTION, seed=42, budget=StubBudget(), workspace_ref="workspace-1", model_timeout_seconds=30, tool_timeout_seconds=5, max_output_tokens=50)


def metrics(candidate=False):
    return ValidationMetrics(score=1 if candidate else 0.5, similar_score=1 if candidate else 0, regression_score=1, success_rate=1 if candidate else 0.5, regression_failure_count=0, task_count=2, similar_count=1, regression_count=1)


def acceptance(accepted=True):
    return AcceptanceResult(candidate_id="candidate-1", accepted=accepted, reward=0.5, reason="improved" if accepted else "regression_guard", delta_score=0.5, delta_similar_score=1, delta_regression_score=0, policy_id="improvement_with_regression_guard", policy_version="v0.1")


def report():
    usage = ResourceUsage(input_tokens=1, output_tokens=1, total_tokens=2, model_calls=1, tool_calls=0, elapsed_seconds=1, usage_complete=True, usage_sources=["simulated"])
    return ValidationReport(validation_id="validation-1", candidate_id="candidate-1", baseline_version_id="v1", candidate_version_id="v2", status="complete", baseline_metrics=metrics(), candidate_metrics=metrics(True), local_result_ref=artifact("local_results"), private_results_ref=artifact("validation_results"), candidate_validation_tokens=2, usage=usage)


def run_result():
    final = execution("v2")
    final_score = evaluation(final, 1, True)
    return RunResult(run_id="run-1", episode_id="episode-1", task_id="dev-001", initial_version_id="v1", final_stable_version_id="v2", status="completed", final_evaluation_status="evaluated", final_execution=final, final_evaluation=final_score, last_evaluated_execution=final, last_evaluated_evaluation=final_score, evolution_rounds_used=1, stop_reason="task_succeeded", stop_stage="post_commit_execution", experience_refs=[RecordRef(record_id="event-1", uri="experience.jsonl#event-1")], usage=ResourceUsage.zero())
