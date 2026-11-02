"""Target decisions, public evolution context and candidate generation DTOs."""

from dataclasses import dataclass

from pydantic import Field, model_validator

from .base import DomainModel, Identifier, Items, JsonObject, PositiveInt, ReadOnlyMap, Reward, UtcTimestamp
from .acceptance import WeightedGain, validate_reward
from .execution import Diagnosis, ErrorInfo, ExecutionView, FeedbackView
from .history import ComponentEdit, ValidationSummary
from .construction import ConstructionFeedback
from .references import AgentSnapshot, ArtifactRef
from .runtime import RunContext, RunContextRecord
from .states import AttemptStatusValue, CandidateStatus, CandidateStatusValue, EvolutionResponseStatus, ResponseStatusValue
from .task import TaskView
from .usage import BudgetRemaining, ResourceUsage
from .skills import SkillDeliveryCapabilities


class AgentCapabilities(DomainModel):
    supported_targets: Items[Identifier]
    supported_artifact_kinds: Items[Identifier]
    can_rebuild_session: bool
    snapshot_scope: Identifier
    feedback_state_fields: Items[Identifier] = ("agent_plugin", "model_steps", "termination_reason", "fresh_session")
    feedback_config_fields: Items[Identifier] = ("max_steps",)
    feedback_tool_arguments: ReadOnlyMap[Items[str]] = Field(default_factory=dict)
    feedback_tool_outputs: ReadOnlyMap[Items[str]] = Field(default_factory=dict)
    skill_delivery: SkillDeliveryCapabilities | None = None


class EvolutionCapabilities(DomainModel):
    supported_targets: Items[Identifier]
    compatible_agents: Items[Identifier]
    required_artifact_kinds: Items[Identifier]
    supports_multiple_candidates: bool
    reports_usage: bool
    target_descriptions: ReadOnlyMap[str] = Field(default_factory=dict)


class EvolutionDecision(DomainModel):
    target: Identifier
    rationale: str
    usage: ResourceUsage | None = None
    record_ref: ArtifactRef | None = None


class CandidateSummary(DomainModel):
    candidate_id: Identifier
    target: Identifier | None = None
    updated_components: Items[Identifier] = ()
    change_summary: str | None = None
    generation_rationale: str | None = None
    actual_changes: Items[ComponentEdit] = ()
    changes_truncated: bool = False
    changes_unavailable: bool = False
    change_summary_truncated: bool = False
    generation_rationale_truncated: bool = False
    validation: ValidationSummary | None = None
    tool_usage: JsonObject | None = None
    candidate_version_id: Identifier | None = None
    reward: Reward | None
    delta_score: Reward | None
    delta_similar_score: Reward | None
    delta_regression_score: Reward | None
    accepted: bool
    selected: bool
    committed: bool
    reason: str
    acceptance_reason: Identifier | None = None
    policy_id: Identifier | None = None
    policy_version: Identifier | None = None
    delta_current_task_score: Reward | None = None
    weighted_gain: WeightedGain | None = None

    @model_validator(mode="after")
    def decision_flags(self):
        if self.selected and not self.accepted or self.committed and not self.selected:
            raise ValueError("committed implies selected; selected implies accepted")
        if self.accepted and (self.reward is None or self.reward <= 0):
            raise ValueError("accepted candidates require a known positive reward")
        validate_reward(self.reward, self.delta_score, self.delta_similar_score, self.delta_regression_score,
                        self.delta_current_task_score, self.weighted_gain)
        return self


class ExperienceSummary(DomainModel):
    attempt_id: Identifier
    run_id: Identifier
    episode_id: Identifier
    task_id: Identifier
    round_id: PositiveInt
    stable_version_id: Identifier
    decision: EvolutionDecision | None
    diagnosis: Diagnosis | None
    candidate_summaries: Items[CandidateSummary]
    completed_at: UtcTimestamp
    stable_version_after: Identifier | None = None
    attempt_status: AttemptStatusValue | None = None
    error_code: Identifier | None = None
    routing_rationale_truncated: bool = False
    construction_feedback: ConstructionFeedback | None = None


class EvolutionContext(DomainModel):
    round_id: PositiveInt = 1
    task: TaskView
    stable_snapshot: AgentSnapshot
    execution: ExecutionView
    feedback: FeedbackView
    diagnosis: Diagnosis | None
    history: Items[ExperienceSummary]
    feasible_targets: Items[Identifier]
    remaining_budget: BudgetRemaining | None
    as_of: UtcTimestamp
    run_context: RunContextRecord | None = None
    target_descriptions: ReadOnlyMap[str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def state_matches_execution(self):
        if self.execution.task_id != self.task.task_id:
            raise ValueError("execution must belong to the current task")
        if self.execution.agent_version_id != self.stable_snapshot.version_id:
            raise ValueError("execution must belong to the stable Snapshot")
        if not self.feasible_targets or len(set(self.feasible_targets)) != len(self.feasible_targets):
            raise ValueError("routing requires unique, non-empty feasible_targets")
        if any(item.completed_at > self.as_of for item in self.history):
            raise ValueError("history cannot contain future experience")
        steps = {event.seq for event in self.execution.trajectory}
        references = set(self.feedback.evidence_step_ids)
        if self.diagnosis is not None:
            references.update(self.diagnosis.all_evidence_step_ids)
        if not references <= steps:
            raise ValueError("diagnosis and feedback evidence must refer to visible trace steps")
        return self


class EvolutionRequestRecord(DomainModel):
    attempt_id: Identifier
    round_id: PositiveInt
    context: EvolutionContext
    decision: EvolutionDecision
    operator_id: Identifier
    max_candidates: PositiveInt
    run_context: RunContextRecord

    @model_validator(mode="before")
    @classmethod
    def legacy_context_round(cls, data):
        if isinstance(data, dict) and isinstance(data.get("context"), dict) and "round_id" not in data["context"]:
            data = {**data, "context": {**data["context"], "round_id": data.get("round_id", 1)}}
        return data

    @model_validator(mode="after")
    def selection_is_feasible(self):
        if self.round_id != self.context.round_id:
            raise ValueError("request round_id must match evolution context")
        if self.decision.target not in self.context.feasible_targets:
            raise ValueError("decision target must be feasible")
        if self.decision.target not in self.context.stable_snapshot.artifacts:
            raise ValueError("selected target requires an existing stable artifact")
        return self


@dataclass(frozen=True)
class EvolutionRequest:
    attempt_id: str
    round_id: int
    context: EvolutionContext
    decision: EvolutionDecision
    operator_id: str
    max_candidates: int
    run_context: RunContext

    def __post_init__(self):
        record = self.to_record()
        for name in EvolutionRequestRecord.model_fields:
            if name != "run_context":
                object.__setattr__(self, name, getattr(record, name))

    def to_record(self) -> EvolutionRequestRecord:
        return EvolutionRequestRecord(
            attempt_id=self.attempt_id, round_id=self.round_id, context=self.context,
            decision=self.decision, operator_id=self.operator_id,
            max_candidates=self.max_candidates, run_context=self.run_context.to_record(),
        )

    @classmethod
    def from_record(cls, record: EvolutionRequestRecord, *, run_context: RunContext):
        if run_context.to_record().replace(remaining_budget=record.run_context.remaining_budget) != record.run_context:
            raise ValueError("reattached RunContext must match the recorded operation")
        return cls(**{
            name: getattr(record, name)
            for name in EvolutionRequestRecord.model_fields if name != "run_context"
        }, run_context=run_context)


class EvolutionCandidate(DomainModel):
    candidate_id: Identifier
    parent_version_id: Identifier
    target: Identifier
    artifact_updates: ReadOnlyMap[ArtifactRef]
    candidate_version_id: Identifier | None = None
    metadata: JsonObject = Field(default_factory=dict)
    status: CandidateStatusValue = CandidateStatus.GENERATED

    @model_validator(mode="after")
    def artifact_candidate(self):
        if not self.artifact_updates:
            raise ValueError("candidates require at least one artifact update")
        if any(key != ref.kind for key, ref in self.artifact_updates.items()):
            raise ValueError("artifact update keys must match their artifact kinds")
        if self.candidate_version_id == self.parent_version_id:
            raise ValueError("candidate version must differ from its parent")
        if self.status != CandidateStatus.GENERATED:
            raise ValueError("generation DTO status must be generated; evaluation owns later states")
        return self


class EvolutionResponse(DomainModel):
    candidates: Items[EvolutionCandidate]
    usage: ResourceUsage
    status: ResponseStatusValue
    error: ErrorInfo | None = None
    record_ref: ArtifactRef | None = None
    construction_feedback: ConstructionFeedback | None = None

    @model_validator(mode="after")
    def response_matches_candidates(self):
        if self.construction_feedback is not None:
            if self.construction_feedback.outcome != self.status:
                raise ValueError("Construction feedback outcome must match evolution status")
            if self.construction_feedback.record_ref != self.record_ref:
                raise ValueError("Construction feedback must reference the same construction receipt")
        ids = [item.candidate_id for item in self.candidates]
        if len(ids) != len(set(ids)):
            raise ValueError("candidate IDs must be unique")
        if self.status == EvolutionResponseStatus.COMPLETED and not self.candidates:
            raise ValueError("completed evolution requires candidates; use empty status")
        if self.status == EvolutionResponseStatus.EMPTY and self.candidates:
            raise ValueError("empty evolution cannot contain candidates")
        if self.status == EvolutionResponseStatus.ERROR and self.error is None:
            raise ValueError("error evolution requires ErrorInfo")
        if self.status in {EvolutionResponseStatus.COMPLETED, EvolutionResponseStatus.EMPTY} and self.error is not None:
            raise ValueError("normal evolution cannot include an error")
        return self
