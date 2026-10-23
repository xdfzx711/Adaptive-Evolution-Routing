"""One reviewable routing/generation attempt, without validation or promotion."""

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from aer_core.identifiers import timestamp_id

from aer_core.contracts.router import RouterPlugin
from aer_core.errors import AERError, BudgetExceeded, ContractError, EvolutionError, RoutingError
from aer_core.plugin_manager import BuiltPlugins, PluginManager
from aer_core.schemas.assessment import TaskExecutionRecord
from aer_core.schemas.evolution import EvolutionDecision, EvolutionRequest, EvolutionResponse
from aer_core.schemas.experience import AttemptFinishedEvent, AttemptStartedEvent, CandidateChange, CandidateEvaluatedEvent, CandidateEvaluation, EvolutionAttempt
from aer_core.schemas.proposal import EvolutionProposalRecord
from aer_core.schemas.runtime import RunContext
from aer_core.schemas.states import RunStage, StopReason
from aer_core.schemas.usage import ResourceUsage
from aer_core.targets import OperatorBinding, OperatorResolver, TargetRegistry
from aer_core.usage import sum_usage
from plugins.storage._files import unique_json
from plugins.storage.layout import RunLayout, compact_plugins, write_manifest

from .bootstrap import create_registry, evolution_bindings
from .execution import PreparedExecution, prepare_execution
from .feedback import context_builder


def load_task_record(path: Path) -> TaskExecutionRecord:
    data = unique_json(path.read_bytes())
    if isinstance(data, dict) and "record" in data:
        data = data["record"]
    return TaskExecutionRecord.model_validate(data)


@dataclass(frozen=True)
class PreparedEvolution:
    execution: PreparedExecution
    router: RouterPlugin | None
    resolver: OperatorResolver
    targets: TargetRegistry
    built_plugins: BuiltPlugins

    def feasible_targets(self, stable):
        return self.targets.feasible(self.execution.agent, stable,
            self.execution.benchmark.loaded_config.config.targets.allowed).targets

    def propose(self, record: TaskExecutionRecord, *, round_id: int) -> EvolutionProposalRecord:
        if type(round_id) is not int or round_id < 1:
            raise ContractError("round_id must be a positive integer", stage="routing")
        config = self.execution.benchmark.loaded_config.config
        storage = self.execution.storage
        stable = storage.version_store.stable()
        if record.run_id != storage.run_id:
            raise ContractError("Execution record belongs to another run", stage="routing")
        task = next((task for task in self.execution.benchmark.execution_tasks if task.identity == record.task), None)
        if task is None or record.execution.agent_version_id != stable.version_id:
            raise ContractError("Execution record must match a selected task and current stable version", stage="routing")
        if record.evaluation is None or record.assessment_error is not None:
            raise ContractError("A completed external assessment is required", stage="routing")

        def skipped(reason):
            return EvolutionProposalRecord(run_id=record.run_id, episode_id=record.episode_id, task_id=record.task.task_id,
                stable_version_id=stable.version_id, round_id=round_id, status="skipped", context=None, attempt=None,
                decision=None, operator_id=None, response=None, stop_reason=reason, experience_refs=[])

        if record.evaluation.success:
            return skipped(StopReason.TASK_SUCCEEDED)
        if config.evolution.mode == "disabled":
            return skipped(StopReason.EVOLUTION_DISABLED)
        if round_id > config.limits.max_evolution_rounds:
            return skipped(StopReason.MAX_EVOLUTION_ROUNDS_REACHED)
        if config.evolution.mode == "manual" and not any(item.round_id == round_id for item in config.routing.manual_decisions):
            return skipped(StopReason.MANUAL_PLAN_EXHAUSTED)
        availability = self.targets.feasible(self.execution.agent, stable, config.targets.allowed)
        if not availability.targets:
            return skipped(StopReason.NO_FEASIBLE_TARGETS)
        # Explicit rounds are continuous within this execution episode.
        history = [event.payload.round_id for event in storage.experience_store.events()
                   if isinstance(event, AttemptStartedEvent) and event.run_id == record.run_id and event.episode_id == record.episode_id]
        if round_id != max(history, default=0) + 1:
            raise ContractError("round_id must be the next unused round in this episode", stage="routing")
        RunLayout(storage.run_root).select(task.task_id, record.episode_id, round_id)
        run_ctx = RunContext(run_id=record.run_id, episode_id=record.episode_id,
            operation_id=timestamp_id("evolution"), stage=RunStage.EVOLUTION, seed=config.experiment.seed,
            budget=None, workspace_ref=str(storage.run_root), model_timeout_seconds=config.limits.model_call_timeout_seconds,
            tool_timeout_seconds=config.limits.tool_call_timeout_seconds, max_output_tokens=config.limits.max_output_tokens_per_call)
        public_task = self.execution.benchmark.benchmark.task_view(task)
        builder = context_builder(self.execution, task,
            history_max_attempts=config.evolution.history_max_attempts,
            history_max_text_chars=config.evolution.history_max_text_chars)
        context = builder.build(public_task, record, stable, run_ctx, round_id=round_id,
            feasible_targets=availability.targets, task_mode=config.state.task_mode,
            allowed_history_task_ids=[item.task_id for item in self.execution.benchmark.benchmark.tasks("dev")])
        context = context.replace(target_descriptions={target: self.resolver.resolve(target).operator.capabilities().target_descriptions.get(target, target)
                                                       for target in context.feasible_targets})
        pre_state = storage.artifact_store.put(context.model_dump_json(indent=2).encode("utf-8"), "evolution_context")
        trace = storage.artifact_store.put(json.dumps([event.model_dump(mode="json") for event in context.execution.trajectory], ensure_ascii=False, indent=2).encode("utf-8"), "public_trajectory")
        attempt_id = timestamp_id("attempt")
        attempt_begin = monotonic()
        started_at = datetime.now(timezone.utc)
        started = EvolutionAttempt(attempt_id=attempt_id, run_id=record.run_id, episode_id=record.episode_id,
            task_id=record.task.task_id, round_id=round_id, stable_version_id=stable.version_id, pre_state_ref=pre_state,
            trajectory_ref=trace, feasible_targets=context.feasible_targets, decision=None, operator_id=None, candidate_ids=[],
            status="started", error=None, budget_before=None, budget_after=None, usage=ResourceUsage.zero(),
            started_at=started_at, finished_at=None, diagnosis=context.diagnosis)
        refs = [storage.experience_store.append(AttemptStartedEvent(event_id=attempt_id + "/started", run_id=record.run_id,
            episode_id=record.episode_id, timestamp=started_at, payload=started))]
        decision, operator_id = None, None
        routing_usage = ResourceUsage.zero()
        begin = monotonic()
        try:
            selected = self.router.select(context)
            if not isinstance(selected, EvolutionDecision):
                raise RoutingError("Router must return EvolutionDecision", stage="routing")
            decision = selected
            routing_usage = decision.usage or ResourceUsage.zero()
            if decision.target not in context.feasible_targets:
                raise ContractError("Router selected a target outside feasible_targets", stage="routing")
            binding = self.resolver.resolve(decision.target)
            operator_id = binding.plugin_id
            request = EvolutionRequest(attempt_id=attempt_id, round_id=round_id, context=context, decision=decision,
                operator_id=operator_id, max_candidates=config.evolution.max_candidates_per_round, run_context=run_ctx)
            response = binding.operator.evolve(request)
            if not isinstance(response, EvolutionResponse):
                raise ContractError("Evolver must return EvolutionResponse", stage="evolution")
            if len(response.candidates) > request.max_candidates or any(candidate.parent_version_id != stable.version_id or
                candidate.target != decision.target for candidate in response.candidates):
                raise ContractError("Evolver candidates must match target, parent and count limit", stage="evolution", usage=response.usage)
        except Exception as failure:
            stage = "routing" if operator_id is None else "evolution"
            if isinstance(failure, AERError):
                usage = failure.to_info().usage
                if usage is None:
                    usage = ResourceUsage.zero().replace(elapsed_seconds=monotonic() - begin) if stage == "routing" else ResourceUsage(
                        input_tokens=None, output_tokens=None, total_tokens=None, model_calls=0, tool_calls=0,
                        elapsed_seconds=monotonic() - begin, usage_complete=False, usage_sources=["unknown"])
                error = failure.to_info().replace(operation_id=run_ctx.operation_id, usage=usage)
            else:
                # A thrown Evolver error without usage cannot claim known zero model cost.
                usage = ResourceUsage.zero().replace(elapsed_seconds=monotonic() - begin) if stage == "routing" else ResourceUsage(
                    input_tokens=None, output_tokens=None, total_tokens=None, model_calls=0, tool_calls=0,
                    elapsed_seconds=monotonic() - begin, usage_complete=False, usage_sources=["unknown"])
                error_type = RoutingError if stage == "routing" else EvolutionError
                error = error_type("Routing or candidate generation failed", stage=stage, operation_id=run_ctx.operation_id, usage=usage).to_info()
            if stage == "routing":
                # No Evolver was invoked. Keep routing costs separate even when
                # the shared failure envelope is an EvolutionResponse.
                routing_usage = sum_usage([routing_usage, usage])
                error = error.replace(usage=routing_usage)
                usage = ResourceUsage.zero()
            response = EvolutionResponse(candidates=[], usage=usage, status="budget_interrupted" if isinstance(failure, BudgetExceeded) else "error", error=error)
        finished_at = datetime.now(timezone.utc)
        status = {"completed": "completed", "empty": "empty_candidates", "error": "error", "budget_interrupted": "budget_interrupted"}[response.status]
        candidate_changes = {}
        for candidate in response.candidates:
            summary = candidate.metadata.get("change_summary")
            rationale = candidate.metadata.get("rationale")
            candidate_changes[candidate.candidate_id] = CandidateChange(target=candidate.target,
                updated_components=tuple(candidate.artifact_updates),
                change_summary=summary[:config.evolution.history_max_text_chars] if isinstance(summary, str) else None,
                generation_rationale=rationale[:config.evolution.history_max_text_chars] if isinstance(rationale, str) else None)
        finished = started.replace(status=status, decision=decision, operator_id=operator_id,
            candidate_ids=[candidate.candidate_id for candidate in response.candidates],
            candidate_changes=candidate_changes,
            usage=sum_usage([routing_usage, response.usage]).replace(elapsed_seconds=monotonic() - attempt_begin),
            error=response.error, finished_at=finished_at)
        refs.append(storage.experience_store.append(AttemptFinishedEvent(event_id=attempt_id + "/finished", run_id=record.run_id,
            episode_id=record.episode_id, timestamp=finished_at, payload=finished)))
        for candidate in response.candidates:
            outcome = CandidateEvaluation(attempt_id=attempt_id, candidate_id=candidate.candidate_id, candidate_version_id=None,
                validation_ref=None, acceptance=None, status="not_validated", selected=False, committed=False,
                commit_error=None, final_recorded_at=finished_at)
            refs.append(storage.experience_store.append(CandidateEvaluatedEvent(event_id=candidate.candidate_id + "/not-validated",
                run_id=record.run_id, episode_id=record.episode_id, timestamp=finished_at, payload=outcome)))
        return EvolutionProposalRecord(run_id=record.run_id, episode_id=record.episode_id, task_id=record.task.task_id,
            stable_version_id=stable.version_id, round_id=round_id, status="finished", context=context, attempt=finished,
            decision=decision, operator_id=operator_id, response=response, stop_reason=None, experience_refs=refs)

    def save_record(self, record: EvolutionProposalRecord) -> dict:
        ref = self.execution.storage.artifact_store.put(record.model_dump_json(indent=2).encode("utf-8"), "evolution_proposal")
        return {"status": "proposal_recorded", "run_root": str(self.execution.storage.run_root),
                "record_ref": ref.model_dump(mode="json"), "record": record.model_dump(mode="json"),
                "notes": ["Candidate generation only; validation, acceptance and commit were not performed."]}


def prepare_evolution(path: str | Path, *, run_id: str) -> PreparedEvolution:
    return assemble_evolution(prepare_execution(path, run_id=run_id))


def assemble_evolution(execution: PreparedExecution) -> PreparedEvolution:
    config = execution.benchmark.loaded_config.config
    registry = create_registry()
    bindings = evolution_bindings(config, registry)
    deps = {"artifact_store": execution.storage.artifact_store}
    for name in ("model", "tools"):
        if name in execution.built_plugins.instances:
            deps[name] = execution.built_plugins.instances[name]
    built = PluginManager(registry).build(bindings, dependencies=deps)
    operators = {target: OperatorBinding(plugin_id, built.instances[f"operator:{target}"])
                 for target, plugin_id in config.targets.default_operators.items()}
    resolver = OperatorResolver(config.targets.default_operators, operators)
    write_manifest(execution.storage.run_root, "evolution", {"plugins": compact_plugins(built.manifest)})
    return PreparedEvolution(execution, built.instances.get("router"), resolver, TargetRegistry(resolver), built)
