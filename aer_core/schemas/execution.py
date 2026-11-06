"""Execution facts, external evaluations and public feedback views."""

from typing import Literal
from pydantic import model_validator

from .base import (
    DomainModel, FiniteFloat, Identifier, Items, JsonObject,
    NonNegativeInt, ReadOnlyMap, Score, UtcTimestamp,
)
from .references import ArtifactRef
from .states import ExecutionStatus, ExecutionStatusValue, StageValue
from .usage import ResourceUsage
from .scoring import ScoreDetails


class ErrorInfo(DomainModel):
    code: Identifier
    stage: StageValue
    message: Identifier
    operation_id: Identifier | None = None
    evidence_ref: ArtifactRef | None = None
    plugin_id: Identifier | None = None
    usage: ResourceUsage | None = None


class TraceEvent(DomainModel):
    seq: NonNegativeInt
    kind: Identifier
    payload: JsonObject
    timestamp: UtcTimestamp | None = None


def check_trajectory(events) -> None:
    seqs = [event.seq for event in events]
    if any(left >= right for left, right in zip(seqs, seqs[1:])):
        raise ValueError("TraceEvent.seq must be unique and strictly increasing")


class ExecutionResult(DomainModel):
    task_id: Identifier
    agent_version_id: Identifier
    operation_id: Identifier
    output: JsonObject
    trajectory: Items[TraceEvent]
    state_summary: JsonObject
    usage: ResourceUsage
    status: ExecutionStatusValue
    artifact_refs: Items[ArtifactRef] = ()
    error: ErrorInfo | None = None

    @model_validator(mode="after")
    def execution_is_consistent(self):
        check_trajectory(self.trajectory)
        if self.status == ExecutionStatus.ERROR and self.error is None:
            raise ValueError("error execution requires ErrorInfo")
        if self.error is not None and self.status not in {ExecutionStatus.ERROR, ExecutionStatus.BUDGET_INTERRUPTED}:
            raise ValueError("normal execution cannot include ErrorInfo")
        if self.error is not None and self.error.operation_id not in {None, self.operation_id}:
            raise ValueError("execution error operation_id must match the execution")
        return self


class PublicCheckResult(DomainModel):
    """Evaluator-authored facts eligible for explicit benchmark disclosure."""

    check_id: Identifier
    status: Literal["passed", "failed", "unknown"]
    requirement: str | None = None
    explanation: str | None = None


class EvaluationResult(DomainModel):
    evaluation_id: Identifier
    task_id: Identifier
    agent_version_id: Identifier
    execution_operation_id: Identifier
    operation_id: Identifier
    score: Score
    success: bool
    metrics: ReadOnlyMap[FiniteFloat]
    feedback: str
    reason_code: Identifier | None = None
    evidence_refs: Items[ArtifactRef] = ()
    usage: ResourceUsage | None = None
    assessment_version: Identifier | None = None
    score_details: ScoreDetails | None = None
    public_checks: Items[PublicCheckResult] = ()

    @model_validator(mode="after")
    def unique_public_checks(self):
        if len({item.check_id for item in self.public_checks}) != len(self.public_checks):
            raise ValueError("Public check IDs must be unique")
        return self


class FeedbackView(DomainModel):
    score: Score
    success: bool
    reason_code: Identifier
    public_feedback: str
    evidence_step_ids: Items[NonNegativeInt] = ()
    assessment_version: Identifier | None = None
    checks: Items[PublicCheckResult] = ()

    @model_validator(mode="after")
    def unique_checks(self):
        if len({item.check_id for item in self.checks}) != len(self.checks):
            raise ValueError("Feedback check IDs must be unique")
        return self


class EvaluationSignal(FeedbackView):
    """Public evaluator verdict with provenance, independent of diagnosis."""

    evaluation_id: Identifier
    execution_operation_id: Identifier
    evaluator_id: Identifier | None = None


class DiagnosisStatement(DomainModel):
    text: Identifier
    evidence_step_ids: Items[NonNegativeInt] = ()
    check_ids: Items[Identifier] = ()


class CheckDiagnosis(DomainModel):
    check_id: Identifier
    status: Literal["passed", "failed", "unknown"]
    hypotheses: Items[DiagnosisStatement] = ()


class Diagnosis(DomainModel):
    reason_code: Identifier
    explanation: str
    evidence_step_ids: Items[NonNegativeInt] = ()
    confidence: Score | None = None
    observations: Items[DiagnosisStatement] = ()
    hypotheses: Items[DiagnosisStatement] = ()
    source: Literal["rule", "model"] = "rule"
    usage: ResourceUsage | None = None
    record_ref: ArtifactRef | None = None
    fact_check: JsonObject | None = None
    evidence_key: Identifier | None = None
    checks: Items[CheckDiagnosis] = ()
    consistency_status: Literal["verified", "corrected", "fallback"] | None = None

    @property
    def all_evidence_step_ids(self):
        return set(self.evidence_step_ids) | {step for item in (*self.observations, *self.hypotheses) for step in item.evidence_step_ids}


class ExecutionView(DomainModel):
    task_id: Identifier
    agent_version_id: Identifier
    output: JsonObject
    trajectory: Items[TraceEvent]
    state_summary: JsonObject
    usage: ResourceUsage

    @model_validator(mode="after")
    def valid_trajectory(self):
        check_trajectory(self.trajectory)
        return self
