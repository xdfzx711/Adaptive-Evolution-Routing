"""Prepare one validation batch, persist evidence, accept and select by ID."""

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from aer_core.identifiers import timestamp_id

from aer_core.errors import AERError, ContractError, ValidationError
from aer_core.history import validation_summary
from aer_core.plugin_manager import BuiltPlugins, PluginManager
from aer_core.schemas.experience import AttemptFinishedEvent, CandidateEvaluatedEvent, CandidateEvaluation
from aer_core.schemas.paired import ValidationBatchResult, ValidationEvidence
from aer_core.schemas.plugins import PluginBinding
from aer_core.schemas.proposal import EvolutionProposalRecord
from aer_core.schemas.runtime import RunContext
from aer_core.schemas.usage import ResourceUsage
from aer_core.schemas.validation import AcceptanceResult, PairSeed, ValidationPlan
from aer_core.task_executor import ScoredTaskExecutor
from aer_core.usage import sum_usage
from plugins.storage._files import FileRoot, canonical_bytes, key_path, unique_json
from plugins.storage.layout import RunLayout, compact_plugins, write_manifest
from plugins.validation.acceptance import select_candidate
from plugins.validation.cache import LocalValidationCache
from plugins.validation.paired import paired_seed
from .bootstrap import create_registry
from .execution import PreparedExecution, prepare_execution
from .reporting import ReportingTaskExecutor
from .conditions import validation_conditions


def load_proposal(path: Path) -> EvolutionProposalRecord:
    data = unique_json(path.read_bytes())
    return EvolutionProposalRecord.model_validate(data.get("record", data))


@dataclass(frozen=True)
class PreparedValidation:
    execution: PreparedExecution
    built_plugins: BuiltPlugins

    def validate(self, proposal: EvolutionProposalRecord) -> ValidationBatchResult:
        storage = self.execution.storage
        config = self.execution.benchmark.loaded_config.config
        if config.evolution.mode == "disabled" or config.benchmark.execution_split != "dev":
            raise ValidationError("Candidate validation requires evolution on development tasks", stage="setup")
        if proposal.run_id != storage.run_id or proposal.status != "finished" or proposal.response.status != "completed":
            raise ValidationError("Validation requires a completed candidate proposal from this run", stage="setup")
        task = next((task for task in self.execution.benchmark.execution_tasks if task.task_id == proposal.task_id), None)
        if task is None:
            raise ValidationError("Proposal task is outside selected development tasks", stage="setup")
        stable = storage.version_store.stable()
        if proposal.stable_version_id != stable.version_id:
            raise ValidationError("Proposal parent is no longer the stable version", stage="setup")
        events = storage.experience_store.events()
        attempts = [event.payload for event in events if isinstance(event, AttemptFinishedEvent) and event.payload.attempt_id == proposal.attempt.attempt_id]
        if len(attempts) != 1 or attempts[0] != proposal.attempt:
            raise ValidationError("Proposal attempt differs from stored generation experience", stage="setup")
        candidates = proposal.response.candidates
        if set(proposal.attempt.candidate_ids) != {item.candidate_id for item in candidates}:
            raise ValidationError("Candidate IDs differ from the stored attempt", stage="setup")
        files = FileRoot(storage.run_root)
        layout = RunLayout(storage.run_root)
        layout.select(proposal.task_id, proposal.episode_id, proposal.round_id)
        batch_path = layout.named("batches", proposal.attempt.attempt_id,
            "validation/batches/" + key_path(proposal.attempt.attempt_id) + ".json", folder="validation", prefix="batch")
        # Repeated standalone commands return the already recorded batch, without new calls/events.
        if files.path(batch_path).exists():
            return ValidationBatchResult.model_validate(files.read_json(batch_path))
        if any(isinstance(event, CandidateEvaluatedEvent) and event.payload.attempt_id == proposal.attempt.attempt_id
               and event.payload.status != "not_validated" for event in events):
            raise ValidationError("Attempt already has validation outcomes; inspect its saved evidence", stage="setup")
        capabilities = self.execution.agent.capabilities()
        materialized, invalid = [], []
        for candidate in candidates:
            try:
                if (candidate.parent_version_id != stable.version_id or candidate.target not in config.targets.allowed or
                    candidate.target not in capabilities.supported_targets or not capabilities.can_rebuild_session or
                    not set(candidate.artifact_updates) <= set(capabilities.supported_artifact_kinds)):
                    raise ContractError("Candidate target/artifacts are unsupported or parent differs", stage="local_validation")
                snapshot = storage.version_store.materialize(stable, candidate)
                materialized.append(candidate.replace(candidate_version_id=snapshot.version_id))
            except AERError as failure:
                invalid.append((candidate, failure.to_info()))
        # Initial execution and validation share cached, verified observations.
        benchmark = self.execution.benchmark
        similar, regression = benchmark.validation_groups(task)
        plan = ValidationPlan(current_task=task, baseline_execution=None, baseline_evaluation=None,
            similar_tasks=similar, regression_tasks=regression,
            pair_seeds=[PairSeed(task=item.identity, seed=paired_seed(config.experiment.seed, task.identity, item.identity))
                        for item in (*similar, *regression)],
            policy_id=config.validation.acceptance_policy, policy_version=config.validation.policy_version)
        run_ctx = RunContext(run_id=proposal.run_id, episode_id=proposal.episode_id,
            operation_id=timestamp_id("validation-batch"), stage="local_validation", seed=config.experiment.seed,
            budget=None, workspace_ref=str(storage.run_root), model_timeout_seconds=config.limits.model_call_timeout_seconds,
            tool_timeout_seconds=config.limits.tool_call_timeout_seconds, max_output_tokens=config.limits.max_output_tokens_per_call)
        reports = self.built_plugins.instances["validation"].validate(stable, tuple(materialized), plan, run_ctx)
        if len({report.candidate_id for report in reports}) != len(reports) or {report.candidate_id for report in reports} != {item.candidate_id for item in materialized}:
            raise ValidationError("Harness returned missing, duplicate or unknown candidate IDs", stage="local_validation")
        current_results = {}
        for report in reports:
            if report.local_result_ref is None:
                continue
            evidence = ValidationEvidence.model_validate_json(storage.artifact_store.get(report.local_result_ref))
            local = [pair.candidate for pair in evidence.results if pair.group == "local"]
            if len(local) != 1 or local[0].task != task.identity or local[0].version_id != report.candidate_version_id or local[0].seed != run_ctx.seed:
                raise ValidationError("Current task evidence differs from candidate/task/seed", stage="local_validation")
            current_results[report.candidate_id] = local[0]
        policy = self.built_plugins.instances["acceptance"]
        acceptances = tuple(policy.evaluate(report) for report in reports)
        selected = select_candidate(reports, acceptances)
        policies = {result.candidate_id: result for result in acceptances}
        now = datetime.now(timezone.utc)
        outcomes, refs = [], []
        for report in reports:
            from aer_core.tool_usage import tool_usage_summary
            local = current_results.get(report.candidate_id)
            tool_usage = tool_usage_summary(local.execution.state_summary) if local and local.execution and "selected_tools" in local.execution.state_summary else None
            validation_ref = storage.version_store.save_validation(report)
            acceptance = policies[report.candidate_id]
            chosen = report.candidate_id == selected
            status = "selected" if chosen else "accepted" if acceptance.accepted else "incomplete" if report.status != "complete" else "rejected"
            outcomes.append(CandidateEvaluation(attempt_id=proposal.attempt.attempt_id, candidate_id=report.candidate_id,
                candidate_version_id=report.candidate_version_id, validation_ref=validation_ref, acceptance=acceptance,
                status=status, selected=chosen, committed=False, commit_error=None, final_recorded_at=now,
                validation=validation_summary(report), tool_usage=tool_usage))
        for candidate, error in invalid:
            acceptance = AcceptanceResult(candidate_id=candidate.candidate_id, accepted=False, reward=None,
                delta_score=None, delta_similar_score=None, delta_regression_score=None, reason="invalid_candidate",
                policy_id=plan.policy_id, policy_version=plan.policy_version)
            outcomes.append(CandidateEvaluation(attempt_id=proposal.attempt.attempt_id, candidate_id=candidate.candidate_id,
                candidate_version_id=None, validation_ref=None, acceptance=acceptance, status="rejected", selected=False,
                committed=False, commit_error=None, validation_error=error, final_recorded_at=now))
        for outcome in outcomes:
            refs.append(storage.experience_store.append(CandidateEvaluatedEvent(event_id=outcome.candidate_id + "/validation-outcome",
                run_id=proposal.run_id, episode_id=proposal.episode_id, timestamp=now, payload=outcome)))
        result = ValidationBatchResult(run_id=proposal.run_id, episode_id=proposal.episode_id,
            attempt_id=proposal.attempt.attempt_id, stable_version_id=stable.version_id, reports=reports, outcomes=outcomes,
            selected_candidate_id=selected, usage=sum_usage(report.usage for report in reports), experience_refs=refs,
            current_task_results=current_results)
        files.immutable(batch_path, canonical_bytes(result.model_dump(mode="json")))
        return result

    def save_record(self, record: ValidationBatchResult) -> dict:
        ref = self.execution.storage.artifact_store.put(record.model_dump_json(indent=2).encode("utf-8"), "validation_batch")
        return {"status": "validation_recorded", "run_root": str(self.execution.storage.run_root),
            "record_ref": ref.model_dump(mode="json"), "record": record.model_dump(mode="json"),
            "notes": ["Accepted and selected are recorded separately. The stable version was not committed.",
                      "Batch usage counts shared baseline calls once and excludes calls reused from cache."]}


def prepare_validation(path: str | Path, *, run_id: str) -> PreparedValidation:
    return assemble_validation(prepare_execution(path, run_id=run_id))


def assemble_validation(execution: PreparedExecution, *, progress=None, conditions=None) -> PreparedValidation:
    config, benchmark = execution.benchmark.loaded_config.config, execution.benchmark
    registry = create_registry()
    validation_config = config.validation.model_dump(mode="json")
    bindings = [PluginBinding(name="validation", category="validation", plugin_id=config.validation.plugin,
        config=validation_config, dependency_bindings={key: key for key in
        ("task_executor", "artifact_store", "version_store", "validation_cache", "conditions")}),
        PluginBinding(name="acceptance", category="acceptance_policy", plugin_id=config.validation.acceptance_policy, config=validation_config)]
    conditions = validation_conditions(execution) if conditions is None else conditions
    executor = ScoredTaskExecutor(execution.agent, benchmark.benchmark, benchmark.evaluator)
    if progress is not None:
        executor = ReportingTaskExecutor(executor, progress)
    built = PluginManager(registry).build(bindings, dependencies={
        "task_executor": executor,
        "artifact_store": execution.storage.artifact_store, "version_store": execution.storage.version_store,
        "validation_cache": LocalValidationCache(execution.storage.run_root), "conditions": conditions})
    write_manifest(execution.storage.run_root, "validation", {
        "plugins": compact_plugins(built.manifest),
        "dataset_version": conditions["dataset_version"], "scorer_version": conditions["scorer_version"],
        "budget_enforced": False})
    return PreparedValidation(execution, built)
