"""Public-input model diagnosis evidence and actual call usage."""

from .base import DomainModel, Identifier, Items, JsonObject, UtcTimestamp
from .execution import Diagnosis, ErrorInfo, ExecutionView, FeedbackView
from .providers import ModelRequest, ModelResponse
from .task import TaskView
from .usage import ResourceUsage


class DiagnosisAttempt(DomainModel):
    request: ModelRequest
    response: ModelResponse | None
    conflicts: Items[str] = ()


class DiagnosisRecord(DomainModel):
    run_id: Identifier
    episode_id: Identifier
    task_id: Identifier
    operation_id: Identifier
    started_at: UtcTimestamp
    finished_at: UtcTimestamp
    task: TaskView
    execution: ExecutionView
    feedback: FeedbackView
    response: ModelResponse | None
    diagnosis: Diagnosis | None
    usage: ResourceUsage
    error: ErrorInfo | None
    fact_check: JsonObject | None = None
    attempts: Items[DiagnosisAttempt] = ()
