"""Append-only, discriminated experience events and candidate outcomes."""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from .base import DomainModel, Identifier, Items, JsonObject, PositiveInt, ReadOnlyMap, UtcTimestamp
from .evolution import EvolutionDecision
from .construction import ConstructionFeedback
from .execution import Diagnosis, ErrorInfo
from .history import ComponentEdit, ValidationSummary
from .experiment import RunResult, TaskFinishedSummary
from .references import AgentSnapshot, ArtifactRef, RecordRef
from .states import AttemptStatus, AttemptStatusValue, CandidateStatus, CandidateStatusValue
from .usage import BudgetRemaining, ResourceUsage
from .validation import AcceptanceResult


class CandidateChange(DomainModel):
    """Public, compact account of what a generated candidate changed."""

    target: Identifier
    updated_components: Items[Identifier]
    change_summary: str | None = None
    generation_rationale: str | None = None
    actual_changes: Items[ComponentEdit] = ()
    changes_unavailable: bool = False


class EvolutionAttempt(DomainModel):
    attempt_id: Identifier
    run_id: Identifier
    episode_id: Identifier
    task_id: Identifier
    round_id: PositiveInt
    stable_version_id: Identifier
    pre_state_ref: ArtifactRef
    trajectory_ref: ArtifactRef
    feasible_targets: Items[Identifier]
    decision: EvolutionDecision | None
    operator_id: Identifier | None
    candidate_ids: Items[Identifier]
    candidate_changes: ReadOnlyMap[CandidateChange] = Field(default_factory=dict)
    status: AttemptStatusValue
    error: ErrorInfo | None
    budget_before: BudgetRemaining | None
    budget_after: BudgetRemaining | None
    usage: ResourceUsage
    started_at: UtcTimestamp
    finished_at: UtcTimestamp | None
    diagnosis: Diagnosis | None = None
    construction_feedback: ConstructionFeedback | None = None

    @model_validator(mode="after")
    def attempt_lifecycle(self):
        if len(set(self.candidate_ids)) != len(self.candidate_ids):
            raise ValueError("candidate IDs must be unique")
        if not set(self.candidate_changes) <= set(self.candidate_ids):
            raise ValueError("candidate changes must belong to generated candidates")
        if self.status == AttemptStatus.STARTED:
            if self.finished_at is not None or self.budget_after is not None:
                raise ValueError("started attempt cannot have terminal time or budget")
            if self.decision is not None or self.operator_id is not None or self.candidate_ids or self.error is not None:
                raise ValueError("attempt_started records the state before routing")
        elif self.finished_at is None or (self.budget_before is not None and self.budget_after is None):
            raise ValueError("finished attempts require finished_at and budget_after")
        if self.finished_at is not None and self.finished_at < self.started_at:
            raise ValueError("finished_at cannot precede started_at")
        if self.status == AttemptStatus.EMPTY_CANDIDATES and self.candidate_ids:
            raise ValueError("empty candidate attempt cannot reference candidates")
        if self.status == AttemptStatus.COMPLETED and not self.candidate_ids:
            raise ValueError("completed attempt requires candidates; use empty_candidates")
        if self.status == AttemptStatus.ERROR and self.error is None:
            raise ValueError("error attempt requires ErrorInfo")
        if self.status in {AttemptStatus.COMPLETED, AttemptStatus.EMPTY_CANDIDATES} and (self.decision is None or self.operator_id is None):
            raise ValueError("normal finished attempt requires decision and operator")
        return self


class CandidateEvaluation(DomainModel):
    attempt_id: Identifier
    candidate_id: Identifier
    candidate_version_id: Identifier | None
    validation_ref: RecordRef | None
    acceptance: AcceptanceResult | None
    status: CandidateStatusValue
    selected: bool
    committed: bool
    commit_error: ErrorInfo | None
    final_recorded_at: UtcTimestamp
    validation_error: ErrorInfo | None = None
    validation: ValidationSummary | None = None
    tool_usage: JsonObject | None = None

    @model_validator(mode="after")
    def candidate_lifecycle(self):
        if self.status in {CandidateStatus.GENERATED, CandidateStatus.VALIDATING}:
            raise ValueError("CandidateEvaluation records terminal or selected outcomes, not generation progress")
        if self.acceptance is not None and self.acceptance.candidate_id != self.candidate_id:
            raise ValueError("acceptance must belong to the same candidate")
        accepted = self.acceptance is not None and self.acceptance.accepted
        if self.selected and not accepted or self.committed and not self.selected:
            raise ValueError("committed implies selected; selected implies accepted")
        if accepted and (self.validation_ref is None or self.candidate_version_id is None):
            raise ValueError("accepted candidates require validation and version references")
        if self.validation_ref is not None and self.candidate_version_id is None:
            raise ValueError("validation reference requires a materialized candidate version")
        if self.status == CandidateStatus.INCOMPLETE and self.acceptance is not None and self.acceptance.reward is not None:
            raise ValueError("incomplete candidate validation cannot supply a known reward")
        expected = (
            CandidateStatus.COMMITTED if self.committed
            else CandidateStatus.COMMIT_FAILED if self.commit_error is not None
            else CandidateStatus.SELECTED if self.selected
            else CandidateStatus.ACCEPTED if accepted
            else CandidateStatus.INCOMPLETE if self.status == CandidateStatus.INCOMPLETE
            else CandidateStatus.REJECTED if self.acceptance is not None
            else self.status
        )
        if self.status != expected:
            raise ValueError("candidate status must match acceptance, selection and commit flags")
        if self.commit_error is not None and (not self.selected or self.committed):
            raise ValueError("commit failure requires a selected, uncommitted candidate")
        if self.status in {CandidateStatus.ACCEPTED, CandidateStatus.SELECTED, CandidateStatus.COMMITTED, CandidateStatus.COMMIT_FAILED} and not accepted:
            raise ValueError("accepted/selected/commit states require accepted policy result")
        if self.status == CandidateStatus.REJECTED and self.acceptance is None:
            raise ValueError("rejected state requires a policy rejection result")
        return self


class CommitRequest(DomainModel):
    run_id: Identifier
    episode_id: Identifier
    attempt_id: Identifier
    candidate_id: Identifier
    expected_stable_version_id: Identifier
    validated_snapshot: AgentSnapshot
    validation_ref: RecordRef
    acceptance: AcceptanceResult

    @model_validator(mode="after")
    def eligible_commit(self):
        if not self.acceptance.accepted or self.acceptance.candidate_id != self.candidate_id:
            raise ValueError("commit requires an accepted result for this candidate")
        if self.validated_snapshot.parent_version_id != self.expected_stable_version_id:
            raise ValueError("commit Snapshot must descend from expected stable version")
        return self


class VersionCommitRecord(DomainModel):
    run_id: Identifier
    episode_id: Identifier
    attempt_id: Identifier
    candidate_id: Identifier
    previous_version_id: Identifier
    committed_version_id: Identifier
    committed_at: UtcTimestamp

    @model_validator(mode="after")
    def distinct_versions(self):
        if self.previous_version_id == self.committed_version_id:
            raise ValueError("commit must promote a distinct candidate version")
        return self


class ExperienceQuery(DomainModel):
    run_id: Identifier
    episode_id: Identifier | None = None
    before_or_at: UtcTimestamp
    task_id: Identifier | None = None


class EventEnvelope(DomainModel):
    event_id: Identifier
    schema_version: Literal["aer.experience.v0.1"] = "aer.experience.v0.1"
    run_id: Identifier
    episode_id: Identifier
    timestamp: UtcTimestamp


class AttemptEvent(EventEnvelope):
    payload: EvolutionAttempt

    @model_validator(mode="after")
    def payload_identity(self):
        if (self.run_id, self.episode_id) != (self.payload.run_id, self.payload.episode_id):
            raise ValueError("event identity must match attempt identity")
        return self


class AttemptStartedEvent(AttemptEvent):
    record_type: Literal["attempt_started"] = "attempt_started"

    @model_validator(mode="after")
    def started_payload(self):
        if self.payload.status != AttemptStatus.STARTED or self.timestamp < self.payload.started_at:
            raise ValueError("attempt_started requires a started attempt and valid event time")
        return self


class AttemptFinishedEvent(AttemptEvent):
    record_type: Literal["attempt_finished"] = "attempt_finished"

    @model_validator(mode="after")
    def finished_payload(self):
        if self.payload.status == AttemptStatus.STARTED or self.timestamp < self.payload.finished_at:
            raise ValueError("attempt_finished requires a terminal attempt and valid event time")
        return self


class CandidateEvaluatedEvent(EventEnvelope):
    record_type: Literal["candidate_evaluated"] = "candidate_evaluated"
    payload: CandidateEvaluation

    @model_validator(mode="after")
    def recorded_payload(self):
        if self.timestamp < self.payload.final_recorded_at:
            raise ValueError("event cannot precede candidate outcome recording")
        return self


class VersionCommittedEvent(EventEnvelope):
    record_type: Literal["version_committed"] = "version_committed"
    payload: VersionCommitRecord

    @model_validator(mode="after")
    def committed_payload(self):
        if (self.run_id, self.episode_id) != (self.payload.run_id, self.payload.episode_id):
            raise ValueError("event identity must match commit identity")
        if self.timestamp < self.payload.committed_at:
            raise ValueError("event cannot precede commit time")
        return self


class TaskFinishedEvent(EventEnvelope):
    record_type: Literal["task_finished"] = "task_finished"
    payload: TaskFinishedSummary | RunResult

    @model_validator(mode="after")
    def task_payload(self):
        if (self.run_id, self.episode_id) != (self.payload.run_id, self.payload.episode_id):
            raise ValueError("event identity must match task result identity")
        return self


ExperienceEvent = Annotated[
    AttemptStartedEvent | AttemptFinishedEvent | CandidateEvaluatedEvent
    | VersionCommittedEvent | TaskFinishedEvent,
    Field(discriminator="record_type"),
]
