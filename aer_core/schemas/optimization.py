"""Public evidence and receipts for an Evolver's isolated internal search."""

from typing import Literal

from .base import DomainModel, Identifier, Items, JsonObject, PositiveInt, Score, UtcTimestamp
from .execution import ErrorInfo, ExecutionView, FeedbackView
from .providers import ModelRequest, ModelResponse
from .task import TaskView
from .usage import ResourceUsage
from .scoring import ScoreDetails


class TrialObservation(DomainModel):
    task: TaskView
    execution: ExecutionView | None
    feedback: FeedbackView | None
    usage: ResourceUsage
    error: ErrorInfo | None = None
    score_details: ScoreDetails | None = None


class OptimizationTrial(DomainModel):
    trial: PositiveInt
    candidate: PositiveInt
    started_at: UtcTimestamp
    finished_at: UtcTimestamp
    observation: TrialObservation


class OptimizationReflection(DomainModel):
    call: PositiveInt
    started_at: UtcTimestamp
    finished_at: UtcTimestamp
    request: ModelRequest
    response: ModelResponse | None
    usage: ResourceUsage
    error: ErrorInfo | None = None


class OptimizationCandidate(DomainModel):
    candidate: PositiveInt
    parents: Items[PositiveInt] = ()
    prompt: str
    score: Score | None = None
    selected: bool = False


class OptimizationRecord(DomainModel):
    schema_version: str = "aer.optimization.v0.1"
    optimizer: Identifier
    engine_version: Identifier
    started_at: UtcTimestamp
    finished_at: UtcTimestamp
    status: Literal["completed", "empty", "error", "budget_interrupted"]
    reason: Identifier
    stop_reason: Identifier = "completed"
    config: JsonObject
    trials: Items[OptimizationTrial]
    reflections: Items[OptimizationReflection]
    candidates: Items[OptimizationCandidate]
    decisions: Items[JsonObject] = ()
    usage: ResourceUsage
    error: ErrorInfo | None = None
