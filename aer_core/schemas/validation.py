"""Paired validation plans and results, separate from acceptance policy code."""

from math import isclose

from pydantic import model_validator

from .base import DomainModel, Identifier, Items, NonNegativeInt, PositiveInt, Reward, Score
from .execution import ErrorInfo, EvaluationResult, ExecutionResult
from .references import ArtifactRef
from .states import ValidationStatus, ValidationStatusValue
from .task import Task, TaskIdentity
from .usage import ResourceUsage


class PairSeed(DomainModel):
    task: TaskIdentity
    seed: NonNegativeInt


def evaluations_match(execution: ExecutionResult, evaluation: EvaluationResult) -> bool:
    return (
        execution.task_id == evaluation.task_id
        and execution.agent_version_id == evaluation.agent_version_id
        and execution.operation_id == evaluation.execution_operation_id
    )


class ValidationPlan(DomainModel):
    current_task: Task
    baseline_execution: ExecutionResult | None
    baseline_evaluation: EvaluationResult | None
    similar_tasks: Items[Task]
    regression_tasks: Items[Task]
    pair_seeds: Items[PairSeed]
    policy_id: Identifier
    policy_version: Identifier

    @model_validator(mode="after")
    def independent_pairing(self):
        if not self.similar_tasks or not self.regression_tasks:
            raise ValueError("similar and regression groups must both be non-empty")
        tasks = (*self.similar_tasks, *self.regression_tasks)
        identities = [(task.benchmark_id, task.task_id) for task in tasks]
        if len(identities) != len(set(identities)):
            raise ValueError("validation groups must be disjoint with unique task identities")
        current = (self.current_task.benchmark_id, self.current_task.task_id)
        if current in identities:
            raise ValueError("current task cannot be in independent validation")
        seed_ids = [(item.task.benchmark_id, item.task.task_id) for item in self.pair_seeds]
        if len(seed_ids) != len(set(seed_ids)) or set(seed_ids) != set(identities):
            raise ValueError("pair_seeds must provide exactly one shared seed per validation task")
        if (self.baseline_execution is None) != (self.baseline_evaluation is None):
            raise ValueError("baseline execution and evaluation must both be supplied or both be null")
        if self.baseline_execution is not None and (self.baseline_execution.task_id != self.current_task.task_id or not evaluations_match(self.baseline_execution, self.baseline_evaluation)):
            raise ValueError("baseline execution and evaluation must match the current task")
        return self


class ValidationMetrics(DomainModel):
    score: Score
    similar_score: Score
    regression_score: Score
    success_rate: Score
    regression_failure_count: NonNegativeInt
    task_count: PositiveInt
    similar_count: PositiveInt
    regression_count: PositiveInt

    @model_validator(mode="after")
    def consistent_counts_and_average(self):
        if self.task_count != self.similar_count + self.regression_count:
            raise ValueError("task_count must equal the sum of group counts")
        expected = (self.similar_score * self.similar_count + self.regression_score * self.regression_count) / self.task_count
        if not isclose(self.score, expected, rel_tol=1e-9, abs_tol=1e-12):
            raise ValueError("score must be the task-weighted group average")
        if self.regression_failure_count > self.regression_count:
            raise ValueError("regression failure count cannot exceed regression task count")
        return self


class LocalValidationMetrics(DomainModel):
    baseline_score: Score
    candidate_score: Score
    baseline_success: bool
    candidate_success: bool
    delta_score: Reward
    repaired: bool

    @model_validator(mode="after")
    def local_difference(self):
        if not isclose(self.delta_score, self.candidate_score - self.baseline_score, abs_tol=1e-12):
            raise ValueError("local delta must match the observed scores")
        if self.repaired != (not self.baseline_success and self.candidate_success):
            raise ValueError("repaired requires baseline failure and candidate success")
        return self


class ValidationReport(DomainModel):
    validation_id: Identifier
    candidate_id: Identifier
    baseline_version_id: Identifier
    candidate_version_id: Identifier
    status: ValidationStatusValue
    baseline_metrics: ValidationMetrics | None
    candidate_metrics: ValidationMetrics | None
    local_result_ref: ArtifactRef | None
    private_results_ref: ArtifactRef | None
    candidate_validation_tokens: NonNegativeInt | None
    usage: ResourceUsage
    error: ErrorInfo | None = None
    local_metrics: LocalValidationMetrics | None = None

    @model_validator(mode="after")
    def complete_report_has_evidence(self):
        if self.baseline_version_id == self.candidate_version_id:
            raise ValueError("validation must compare distinct versions")
        if self.status == ValidationStatus.COMPLETE:
            if any(value is None for value in (self.baseline_metrics, self.candidate_metrics, self.local_result_ref, self.private_results_ref, self.candidate_validation_tokens)):
                raise ValueError("complete validation requires both metrics, evidence and candidate token usage")
            if not self.usage.usage_complete or self.error is not None:
                raise ValueError("complete validation requires complete usage and no error")
            baseline, candidate = self.baseline_metrics, self.candidate_metrics
            if (baseline.task_count, baseline.similar_count, baseline.regression_count) != (candidate.task_count, candidate.similar_count, candidate.regression_count):
                raise ValueError("paired versions must use identical group counts")
            if self.candidate_validation_tokens > self.usage.total_tokens:
                raise ValueError("candidate token usage cannot exceed total validation token usage")
        if self.status == ValidationStatus.ERROR and self.error is None:
            raise ValueError("error validation requires ErrorInfo")
        return self


class AcceptanceResult(DomainModel):
    candidate_id: Identifier
    accepted: bool
    reward: Reward | None
    reason: Identifier
    delta_score: Reward | None
    delta_similar_score: Reward | None
    delta_regression_score: Reward | None
    policy_id: Identifier
    policy_version: Identifier

    @model_validator(mode="after")
    def reward_matches_independent_gain(self):
        deltas = (self.delta_score, self.delta_similar_score, self.delta_regression_score)
        if self.reward is None:
            if any(value is not None for value in deltas) or self.accepted:
                raise ValueError("unknown reward requires null deltas and cannot be accepted")
        elif any(value is None for value in deltas) or not isclose(self.reward, self.delta_score, abs_tol=1e-12):
            raise ValueError("known reward must equal delta_score with all deltas available")
        if self.accepted and self.reward <= 0:
            raise ValueError("accepted candidates require a positive independent gain")
        return self
