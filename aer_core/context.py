"""Permission-filtered evolution state; never retrieve private evaluation artifacts."""

from datetime import datetime, timezone

from aer_core.contracts.storage import ExperienceStore
from aer_core.errors import ContractError, NoFeasibleTargets
from aer_core.history import limit_edits
from aer_core.construction_feedback import limit_construction_feedback
from aer_core.schemas.assessment import TaskExecutionRecord
from aer_core.schemas.evolution import CandidateSummary, EvolutionContext, EvolutionDecision, ExperienceSummary
from aer_core.schemas.feedback import ContextPolicy
from aer_core.feedback import PUBLIC_REASONS, PublicFeedbackBuilder
from aer_core.schemas.experience import AttemptFinishedEvent, CandidateEvaluatedEvent, ExperienceQuery, TaskFinishedEvent, VersionCommittedEvent
from aer_core.schemas.references import AgentSnapshot
from aer_core.schemas.runtime import RunContext
from aer_core.schemas.task import TaskView


PUBLIC_ACCEPTANCE_REASONS = frozenset({
    "improved", "no_improvement", "insufficient_improvement", "regression_guard",
    "validation_error", "validation_incomplete", "invalid_candidate", "missing_local_metrics",
})


class ContextBuilder:
    def __init__(self, experience_store: ExperienceStore, policy: ContextPolicy):
        self.experience = experience_store
        self.policy = policy
        self.feedback_builder = PublicFeedbackBuilder(policy)

    def public_views(self, task, execution, evaluation):
        return self.feedback_builder.public_views(task, execution, evaluation)

    def feedback(self, evaluation):
        return self.feedback_builder.feedback(evaluation)

    def _diagnosis(self, diagnosis, visible_steps):
        return self.feedback_builder.diagnosis_view(diagnosis, visible_steps)

    def _history(self, record, round_id, task_mode, as_of, allowed_task_ids):
        events = self.experience.query(ExperienceQuery(run_id=record.run_id,
            episode_id=record.episode_id if task_mode == "independent" else None,
            task_id=record.task.task_id if task_mode == "independent" else None, before_or_at=as_of))
        # Check scopes even if a custom Store returns more than the requested query.
        events = [event for event in events if event.run_id == record.run_id and event.timestamp <= as_of]
        finished_episodes = {(event.episode_id, event.payload.task_id) for event in events if isinstance(event, TaskFinishedEvent)}
        terminals = {}
        for event in events:
            if not isinstance(event, AttemptFinishedEvent) or event.payload.finished_at > as_of:
                continue
            attempt = event.payload
            if attempt.task_id not in allowed_task_ids:
                continue
            if task_mode == "independent" and (attempt.episode_id != record.episode_id or attempt.task_id != record.task.task_id):
                continue
            if attempt.episode_id == record.episode_id:
                if attempt.task_id != record.task.task_id or attempt.round_id >= round_id:
                    continue
            elif (attempt.episode_id, attempt.task_id) not in finished_episodes:
                continue
            terminals[(attempt.episode_id, attempt.attempt_id)] = attempt
        outcomes, commits = {}, {}
        for event in events:
            key = (event.episode_id, event.payload.attempt_id) if isinstance(event, (CandidateEvaluatedEvent, VersionCommittedEvent)) else None
            if key not in terminals:
                continue
            if isinstance(event, CandidateEvaluatedEvent):
                if event.payload.final_recorded_at <= as_of:
                    outcomes[(key, event.payload.candidate_id)] = event.payload
            elif isinstance(event, VersionCommittedEvent) and event.payload.committed_at <= as_of:
                commits[(key, event.payload.candidate_id)] = event.payload
        summaries = []
        for key, attempt in terminals.items():
            candidates = []
            stable_after = attempt.stable_version_id
            for candidate_id in attempt.candidate_ids:
                result = outcomes.get((key, candidate_id))
                change = attempt.candidate_changes.get(candidate_id)
                acceptance = result.acceptance if result is not None else None
                accepted = acceptance is not None and acceptance.accepted
                selected = result.selected if result is not None else False
                committed = accepted and selected and ((key, candidate_id) in commits or result.committed)
                receipt = commits.get((key, candidate_id))
                candidate_version = result.candidate_version_id if result else None
                if committed:
                    stable_after = receipt.committed_version_id if receipt else candidate_version
                changes, truncated = limit_edits(
                    [edit for edit in change.actual_changes if edit.component in self.policy.artifact_kinds]
                    if change else (), self.policy.history_max_change_chars)
                candidates.append(CandidateSummary(candidate_id=candidate_id,
                    target=change.target if change else None,
                    updated_components=change.updated_components if change else (),
                    change_summary=change.change_summary[:self.policy.history_max_text_chars] if change and change.change_summary else None,
                    generation_rationale=change.generation_rationale[:self.policy.history_max_text_chars] if change and change.generation_rationale else None,
                    actual_changes=changes, changes_truncated=truncated,
                    changes_unavailable=change.changes_unavailable if change else False,
                    change_summary_truncated=bool(change and change.change_summary and len(change.change_summary) > self.policy.history_max_text_chars),
                    generation_rationale_truncated=bool(change and change.generation_rationale and len(change.generation_rationale) > self.policy.history_max_text_chars),
                    validation=result.validation if result else None, candidate_version_id=candidate_version,
                    tool_usage=result.model_dump(mode="json")["tool_usage"] if result and "tool" in self.policy.artifact_kinds else None,
                    reward=acceptance.reward if acceptance else None,
                    delta_score=acceptance.delta_score if acceptance else None,
                    delta_similar_score=acceptance.delta_similar_score if acceptance else None,
                    delta_regression_score=acceptance.delta_regression_score if acceptance else None,
                    delta_current_task_score=acceptance.delta_current_task_score if acceptance else None,
                    weighted_gain=acceptance.weighted_gain if acceptance else None,
                    policy_id=acceptance.policy_id if acceptance else None,
                    policy_version=acceptance.policy_version if acceptance else None,
                    accepted=accepted, selected=selected, committed=committed,
                    reason=result.status.value if result else "not_validated",
                    acceptance_reason=(acceptance.reason if acceptance.reason in PUBLIC_ACCEPTANCE_REASONS else "unknown")
                        if acceptance else None))
            decision = EvolutionDecision(target=attempt.decision.target,
                rationale=attempt.decision.rationale[:self.policy.history_max_text_chars]) if attempt.decision else None
            summaries.append(ExperienceSummary(attempt_id=attempt.attempt_id, run_id=attempt.run_id,
                episode_id=attempt.episode_id, task_id=attempt.task_id, round_id=attempt.round_id,
                stable_version_id=attempt.stable_version_id, decision=decision, diagnosis=self._diagnosis(attempt.diagnosis, ()),
                candidate_summaries=candidates, completed_at=attempt.finished_at,
                stable_version_after=stable_after, attempt_status=attempt.status,
                error_code=attempt.error.code if attempt.error else None,
                construction_feedback=limit_construction_feedback(attempt.construction_feedback,
                    self.policy.history_max_construction_chars) if "tool" in self.policy.artifact_kinds else None,
                routing_rationale_truncated=bool(attempt.decision and len(attempt.decision.rationale) > self.policy.history_max_text_chars)))
        ordered = sorted(summaries, key=lambda item: (item.completed_at, item.episode_id, item.round_id, item.attempt_id))
        return tuple(ordered[-self.policy.history_max_attempts:]) if self.policy.history_max_attempts else ()

    def build(self, task: TaskView, record: TaskExecutionRecord, stable: AgentSnapshot,
              run_ctx: RunContext, *, round_id: int, feasible_targets, task_mode="independent",
              as_of: datetime | None = None, allowed_history_task_ids=()) -> EvolutionContext:
        if task_mode not in {"independent", "continual"}:
            raise ContractError("Unknown history task mode", stage="routing")
        if not self.policy.allow_evolution:
            raise ContractError("Evolution context is restricted to development tasks", stage="routing")
        if task.task_id != record.task.task_id or task.benchmark_id != record.task.benchmark_id:
            raise ContractError("Public task and execution record identities must match", stage="routing")
        if (run_ctx.run_id, run_ctx.episode_id) != (record.run_id, record.episode_id):
            raise ContractError("RunContext must match execution record scope", stage="routing")
        if record.evaluation is None or record.assessment_error is not None:
            raise ContractError("Evolution requires a completed external assessment", stage="routing")
        if stable.version_id != record.execution.agent_version_id:
            raise ContractError("Execution must belong to current stable version", stage="routing")
        if not feasible_targets:
            raise NoFeasibleTargets("No executable evolution targets", stage="routing")
        cutoff = as_of or datetime.now(timezone.utc)
        task_view, view, feedback = self.public_views(task, record.execution, record.evaluation)
        trace = view.trajectory
        steps = {event.seq for event in trace}
        controlled = stable.model_dump(mode="json")["config"]
        snapshot = AgentSnapshot(version_id=stable.version_id, agent_plugin_id=stable.agent_plugin_id,
            parent_version_id=stable.parent_version_id,
            config={key: controlled[key] for key in self.policy.snapshot_config_fields if key in controlled},
            artifacts={key: ref for key, ref in stable.artifacts.items() if ref.kind in self.policy.artifact_kinds})
        return EvolutionContext(round_id=round_id, task=task_view, stable_snapshot=snapshot,
            execution=view, feedback=feedback, diagnosis=self._diagnosis(record.diagnosis, steps),
            history=self._history(record, round_id, task_mode, cutoff, set(allowed_history_task_ids) | {task.task_id}),
            feasible_targets=feasible_targets, remaining_budget=run_ctx.budget.remaining() if run_ctx.budget else None,
            as_of=cutoff, run_context=run_ctx.to_record())
