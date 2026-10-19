"""Execution evidence and a batch outcome for isolated paired validation."""

from typing import Literal
from pydantic import model_validator

from .base import DomainModel, Identifier, Items, JsonObject, NonNegativeInt
from .execution import ErrorInfo, EvaluationResult, ExecutionResult
from .experience import CandidateEvaluation
from .references import RecordRef
from .task import TaskIdentity
from .usage import ResourceUsage
from .validation import ValidationReport, evaluations_match


class ScoredExecution(DomainModel):
    task: TaskIdentity
    version_id: Identifier
    seed: NonNegativeInt
    execution: ExecutionResult | None
    evaluation: EvaluationResult | None
    usage: ResourceUsage
    error: ErrorInfo | None

    @model_validator(mode="after")
    def identities(self):
        if self.execution is not None and (self.execution.task_id != self.task.task_id or self.execution.agent_version_id != self.version_id):
            raise ValueError("execution must match task and Snapshot")
        if self.evaluation is not None and (self.execution is None or not evaluations_match(self.execution, self.evaluation)):
            raise ValueError("evaluation must match execution")
        if self.evaluation is not None and self.execution.status not in {"completed", "step_limit"}:
            raise ValueError("only scoreable executions may carry an evaluation")
        if self.evaluation is None and self.error is None:
            raise ValueError("unscored execution requires an explicit error")
        return self

    @property
    def complete(self) -> bool:
        return self.evaluation is not None and self.error is None and self.usage.usage_complete


class PairedTaskResult(DomainModel):
    group: Literal["local", "similar", "regression"]
    baseline: ScoredExecution
    candidate: ScoredExecution
    baseline_cached: bool

    @model_validator(mode="after")
    def paired(self):
        if self.baseline.task != self.candidate.task or self.baseline.seed != self.candidate.seed:
            raise ValueError("paired executions require identical tasks and seeds")
        if self.baseline.version_id == self.candidate.version_id:
            raise ValueError("paired executions require distinct versions")
        return self


class ValidationEvidence(DomainModel):
    conditions: JsonObject
    results: Items[PairedTaskResult]


class ValidationBatchResult(DomainModel):
    run_id: Identifier
    episode_id: Identifier
    attempt_id: Identifier
    stable_version_id: Identifier
    reports: Items[ValidationReport]
    outcomes: Items[CandidateEvaluation]
    selected_candidate_id: Identifier | None
    usage: ResourceUsage
    experience_refs: Items[RecordRef]

    @model_validator(mode="after")
    def association(self):
        report_ids = [report.candidate_id for report in self.reports]
        ids = [outcome.candidate_id for outcome in self.outcomes]
        if len(set(ids)) != len(ids) or len(set(report_ids)) != len(report_ids) or not set(report_ids) <= set(ids):
            raise ValueError("batch candidate IDs must be unique and reports must belong to outcomes")
        if any(report.baseline_version_id != self.stable_version_id for report in self.reports):
            raise ValueError("reports must use the batch stable version")
        outcomes = {outcome.candidate_id: outcome for outcome in self.outcomes}
        if any(outcomes[report.candidate_id].candidate_version_id != report.candidate_version_id or
               outcomes[report.candidate_id].validation_ref is None for report in self.reports):
            raise ValueError("each report requires a matching version and persisted validation reference")
        if any(outcome.attempt_id != self.attempt_id or outcome.committed for outcome in self.outcomes):
            raise ValueError("validation outcomes must match attempt and remain uncommitted")
        selected = [outcome.candidate_id for outcome in self.outcomes if outcome.selected]
        if selected != ([] if self.selected_candidate_id is None else [self.selected_candidate_id]):
            raise ValueError("selected candidate must match the unique selected outcome")
        return self
