"""One Agent execution and its external assessment, before evolution routing."""

from pydantic import Field, model_validator

from .base import DomainModel, Identifier
from .execution import Diagnosis, ErrorInfo, EvaluationResult, ExecutionResult, FeedbackView
from .task import TaskIdentity
from .validation import evaluations_match
from .usage import ResourceUsage


class TaskExecutionRecord(DomainModel):
    run_id: Identifier
    episode_id: Identifier
    task: TaskIdentity
    execution: ExecutionResult
    evaluation: EvaluationResult | None
    feedback: FeedbackView | None
    diagnosis: Diagnosis | None
    assessment_error: ErrorInfo | None = None
    assessment_usage: ResourceUsage = Field(default_factory=ResourceUsage.zero)
    execution_usage: ResourceUsage | None = None
    execution_reused: bool = False

    @model_validator(mode="after")
    def association(self):
        if self.execution.task_id != self.task.task_id:
            raise ValueError("record task and execution must match")
        if self.evaluation is not None and not evaluations_match(self.execution, self.evaluation):
            raise ValueError("evaluation must match task, version and execution operation")
        if self.execution.status in {"error", "budget_interrupted"} and self.evaluation is not None:
            raise ValueError("interrupted/error execution cannot have a normal evaluation")
        if self.execution.status in {"completed", "step_limit"} and self.evaluation is None and self.assessment_error is None:
            raise ValueError("scoreable execution requires evaluation or an assessment error")
        if self.feedback is not None and self.evaluation is None:
            raise ValueError("feedback requires external evaluation")
        if self.evaluation is not None and self.feedback is None and self.assessment_error is None:
            raise ValueError("missing feedback requires an assessment error")
        if self.feedback is not None and (self.feedback.score != self.evaluation.score or self.feedback.success != self.evaluation.success):
            raise ValueError("feedback must match external evaluation")
        if self.diagnosis is not None:
            if self.feedback is None:
                raise ValueError("diagnosis requires external feedback")
            seqs = {event.seq for event in self.execution.trajectory}
            if not self.diagnosis.all_evidence_step_ids.issubset(seqs):
                raise ValueError("diagnosis evidence must reference actual trajectory events")
        return self
