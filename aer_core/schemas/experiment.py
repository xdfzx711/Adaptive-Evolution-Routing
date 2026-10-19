"""Version-associated task output and experiment result contracts."""

from typing import Literal

from pydantic import model_validator

from .base import DomainModel, Identifier, Items, JsonObject, NonNegativeInt, Score
from .execution import ErrorInfo, EvaluationResult, ExecutionResult
from .references import ArtifactRef, ManifestRef, RecordRef
from .states import RunStatus, RunStatusValue, StageValue, StopReason, StopReasonValue
from .usage import ResourceUsage
from .validation import evaluations_match


class RunResult(DomainModel):
    run_id: Identifier
    episode_id: Identifier
    task_id: Identifier
    initial_version_id: Identifier
    final_stable_version_id: Identifier
    status: RunStatusValue
    final_evaluation_status: Literal["evaluated", "not_evaluated"]
    final_execution: ExecutionResult | None
    final_evaluation: EvaluationResult | None
    last_evaluated_execution: ExecutionResult | None
    last_evaluated_evaluation: EvaluationResult | None
    evolution_rounds_used: NonNegativeInt
    stop_reason: StopReasonValue
    stop_stage: StageValue
    experience_refs: Items[RecordRef]
    usage: ResourceUsage
    error: ErrorInfo | None = None

    @model_validator(mode="after")
    def final_version_has_its_own_score(self):
        if self.status in {RunStatus.CREATED, RunStatus.RUNNING}:
            raise ValueError("RunResult must have a terminal run status")
        if self.final_evaluation_status == "evaluated":
            if self.final_execution is None or self.final_evaluation is None:
                raise ValueError("evaluated final state requires execution and evaluation")
            if self.final_execution.agent_version_id != self.final_stable_version_id:
                raise ValueError("final execution must belong to final_stable_version_id")
            if not evaluations_match(self.final_execution, self.final_evaluation):
                raise ValueError("final evaluation must match task, version and execution operation")
        elif self.final_execution is not None or self.final_evaluation is not None:
            raise ValueError("not_evaluated final state must have null final results")
        if (self.last_evaluated_execution is None) != (self.last_evaluated_evaluation is None):
            raise ValueError("last evaluated execution and evaluation must be present together")
        if self.last_evaluated_execution is not None and not evaluations_match(self.last_evaluated_execution, self.last_evaluated_evaluation):
            raise ValueError("last evaluated result pair must match")
        if self.final_evaluation_status == "evaluated" and (
            self.last_evaluated_execution != self.final_execution
            or self.last_evaluated_evaluation != self.final_evaluation
        ):
            raise ValueError("last evaluated pair must be the final pair when the final version was evaluated")
        for result in (self.final_execution, self.last_evaluated_execution):
            if result is not None and result.task_id != self.task_id:
                raise ValueError("all task executions must belong to RunResult.task_id")
        if self.stop_reason == StopReason.TASK_SUCCEEDED and (self.final_evaluation is None or not self.final_evaluation.success):
            raise ValueError("task_succeeded requires successful final external evaluation")
        expected_status = (
            RunStatus.BUDGET_INTERRUPTED if self.stop_reason == StopReason.BUDGET_EXHAUSTED
            else RunStatus.ERROR if self.stop_reason.value.endswith("_error")
            else RunStatus.COMPLETED
        )
        if self.status != expected_status:
            raise ValueError("run status must agree with stop_reason")
        if self.status == RunStatus.ERROR and self.error is None:
            raise ValueError("error run requires ErrorInfo")
        if self.status == RunStatus.COMPLETED and self.error is not None:
            raise ValueError("normal completed run cannot include a runtime error")
        return self


class TaskFinishedSummary(DomainModel):
    """Compact experience payload; full executions live in the referenced artifact."""

    run_id: Identifier
    episode_id: Identifier
    task_id: Identifier
    initial_version_id: Identifier
    final_stable_version_id: Identifier
    status: RunStatusValue
    final_evaluation_status: Literal["evaluated", "not_evaluated"]
    final_score: Score | None
    final_success: bool | None
    evolution_rounds_used: NonNegativeInt
    stop_reason: StopReasonValue
    stop_stage: StageValue
    usage: ResourceUsage
    error: ErrorInfo | None
    result_ref: ArtifactRef

    @model_validator(mode="after")
    def summary_is_terminal(self):
        evaluated = self.final_evaluation_status == "evaluated"
        if evaluated != (self.final_score is not None and self.final_success is not None):
            raise ValueError("evaluated summary requires final score and success")
        if not evaluated and (self.final_score is not None or self.final_success is not None):
            raise ValueError("unevaluated summary cannot supply a final score")
        expected = (RunStatus.BUDGET_INTERRUPTED if self.stop_reason == StopReason.BUDGET_EXHAUSTED
            else RunStatus.ERROR if self.stop_reason.value.endswith("_error") else RunStatus.COMPLETED)
        if self.status != expected or self.status == RunStatus.ERROR and self.error is None:
            raise ValueError("summary status must agree with stop reason and error")
        if self.status == RunStatus.COMPLETED and self.error is not None:
            raise ValueError("completed summary cannot include an error")
        if self.stop_reason == StopReason.TASK_SUCCEEDED and self.final_success is not True:
            raise ValueError("task_succeeded summary requires a successful final evaluation")
        if self.result_ref.kind != "run_result":
            raise ValueError("summary must reference a full run_result artifact")
        return self

    @classmethod
    def from_result(cls, result: RunResult, result_ref: ArtifactRef):
        fields = {key: value for key, value in result.model_dump(mode="json").items() if key in cls.model_fields}
        return cls(**fields, final_score=result.final_evaluation.score if result.final_evaluation else None,
            final_success=result.final_evaluation.success if result.final_evaluation else None, result_ref=result_ref)


class ExperimentResult(DomainModel):
    task_id: Identifier
    method_id: Identifier
    run_id: Identifier
    episode_id: Identifier
    seed: NonNegativeInt
    mode: Literal["disabled", "fixed", "manual"]
    task_mode: Literal["independent", "continual"]
    initial_version_id: Identifier
    final_stable_version_id: Identifier
    run_result_ref: RecordRef
    eval_metrics: JsonObject | None
    resource_usage: ResourceUsage
    trace_refs: Items[ArtifactRef]
    artifact_refs: Items[ArtifactRef]
    provenance: ManifestRef
