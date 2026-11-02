"""Synchronous paired evaluation of arbitrary artifact-based candidates."""

from dataclasses import replace
from hashlib import sha256

from aer_core.cached_executor import CachedTaskExecutor

from aer_core.contracts.storage import ArtifactStore, VersionStore
from aer_core.contracts.validation import ValidationCache, ValidationTaskExecutor
from aer_core.errors import AERError, ValidationError
from aer_core.identifiers import timestamp_id
from aer_core.schemas.paired import PairedTaskResult, ScoredExecution, ValidationEvidence
from aer_core.schemas.usage import ResourceUsage
from aer_core.schemas.validation import LocalValidationMetrics, ValidationMetrics, ValidationPlan, ValidationReport
from aer_core.usage import sum_usage
from aer_core.skill_usage import paired_skill_usage
from plugins.skills.library import decode_library
from plugins.storage._files import canonical_bytes


def paired_seed(base_seed: int, current, task, repeat: int = 0) -> int:
    value = {"seed": base_seed, "current": current.model_dump(mode="json"),
             "task": task.model_dump(mode="json"), "repeat": repeat}
    return int.from_bytes(sha256(canonical_bytes(value)).digest()[:4], "big")


def metrics(results, baseline=None) -> ValidationMetrics:
    similar = [item for group, item in results if group == "similar"]
    regression = [item for group, item in results if group == "regression"]
    entries = similar + regression
    lost = 0
    if baseline is not None:
        old = {(item.task.benchmark_id, item.task.task_id): item for group, item in baseline if group == "regression"}
        lost = sum(old[(item.task.benchmark_id, item.task.task_id)].evaluation.success and not item.evaluation.success for item in regression)
    return ValidationMetrics(score=sum(item.evaluation.score for item in entries) / len(entries),
        similar_score=sum(item.evaluation.score for item in similar) / len(similar),
        regression_score=sum(item.evaluation.score for item in regression) / len(regression),
        success_rate=sum(item.evaluation.success for item in entries) / len(entries),
        regression_failure_count=lost, task_count=len(entries), similar_count=len(similar), regression_count=len(regression))


class PairedValidation:
    plugin_id = "paired_validation"

    def __init__(self, executor: ValidationTaskExecutor, artifacts: ArtifactStore, versions: VersionStore,
                 cache: ValidationCache, conditions: dict):
        self.executor, self.artifacts, self.versions, self.cache = executor, artifacts, versions, cache
        # Own a serializable, immutable conditions record; caller mutations cannot change a cache key.
        self.conditions = ValidationEvidence(conditions=conditions, results=[])
        self.cached_executor = CachedTaskExecutor(executor, cache, conditions)

    def _execute(self, task, snapshot, context):
        result = None
        try:
            result = self.cached_executor.execute(task, snapshot, context)
            if not isinstance(result, ScoredExecution) or (result.task != task.identity or
                result.version_id != snapshot.version_id or result.seed != context.seed):
                raise ValidationError("Executor returned mismatched task/version/seed evidence", stage=context.stage)
            return result
        except Exception as failure:
            error = failure.to_info() if isinstance(failure, AERError) else ValidationError(
                "Validation execution failed", stage=context.stage, operation_id=context.operation_id).to_info()
            usage = result.usage if isinstance(result, ScoredExecution) else error.usage
            if usage is None:
                usage = ResourceUsage(input_tokens=None, output_tokens=None, total_tokens=None,
                    model_calls=0, tool_calls=0, elapsed_seconds=0, usage_complete=False, usage_sources=["unknown"])
            return ScoredExecution(task=task.identity, version_id=snapshot.version_id, seed=context.seed,
                execution=None, evaluation=None, usage=usage, error=error)

    def validate(self, stable, candidates, plan: ValidationPlan, run_ctx) -> tuple[ValidationReport, ...]:
        if not candidates:
            return ()
        if len({item.candidate_id for item in candidates}) != len(candidates):
            raise ValidationError("Duplicate candidate IDs", stage="local_validation")
        if (plan.baseline_execution is not None and plan.baseline_execution.agent_version_id != stable.version_id) or stable != self.versions.load(stable.version_id) or stable != self.versions.stable():
            raise ValidationError("Validation baseline differs from saved Snapshot", stage="local_validation")
        snapshots = {}
        for candidate in candidates:
            if candidate.candidate_version_id is None or candidate.parent_version_id != stable.version_id:
                raise ValidationError("Candidate must be materialized from the baseline", stage="local_validation")
            snapshot = self.versions.load(candidate.candidate_version_id)
            expected = dict(stable.artifacts)
            expected.update(candidate.artifact_updates)
            if (snapshot.parent_version_id != stable.version_id or snapshot.artifacts != expected or
                snapshot.config != stable.config or snapshot.agent_plugin_id != stable.agent_plugin_id):
                raise ValidationError("Candidate Snapshot differs from its declared artifact updates", stage="local_validation")
            snapshots[candidate.candidate_id] = snapshot
        seeds = {(item.task.benchmark_id, item.task.task_id): item.seed for item in plan.pair_seeds}
        jobs = [("local", plan.current_task, run_ctx.seed)] + [
            (group, task, seeds[(task.benchmark_id, task.task_id)])
            for group, tasks in (("similar", plan.similar_tasks), ("regression", plan.regression_tasks)) for task in tasks]
        condition_data = self.conditions.model_dump(mode="json")["conditions"]
        baseline, cached, charged = [], [], []
        for group, task, seed in jobs:
            context = replace(run_ctx, stage=group + "_validation", seed=seed,
                operation_id=run_ctx.operation_id + "/baseline/" + group + "/" + task.benchmark_id + "/" + task.task_id)
            previous = self._execute(task, stable, context)
            charged.append(previous.charged_usage)
            baseline.append((group, previous))
            cached.append(previous.cache_hit)
        baseline_complete = all(item.complete for _, item in baseline)
        reports = []
        for index, candidate in enumerate(candidates):
            version = snapshots[candidate.candidate_id]
            skill_libraries = (decode_library(self.artifacts.get(stable.artifacts["skill"])),
                decode_library(self.artifacts.get(version.artifacts["skill"]))) if candidate.target == "skill" else None
            pairs, candidate_costs = [], []
            for (group, task, seed), (_, old), hit in zip(jobs, baseline, cached):
                context = replace(run_ctx, stage=group + "_validation", seed=seed,
                    operation_id=run_ctx.operation_id + "/" + candidate.candidate_id + "/" + task.benchmark_id + "/" + task.task_id)
                new = self._execute(task, version, context)
                candidate_costs.append(new.charged_usage)
                skill_usage = paired_skill_usage(old, new, stable, version, *skill_libraries) if skill_libraries else None
                if skill_usage and not skill_usage["integrity_ok"]:
                    # Wrong-version reads invalidate the comparison; absence of
                    # reads only limits attribution and does not invent a score.
                    new = new.replace(error=ValidationError("Skill validation evidence does not match the expected Snapshot/library/content",
                        stage=context.stage, operation_id=context.operation_id).to_info())
                pairs.append(PairedTaskResult(group=group, baseline=old, candidate=new, baseline_cached=hit,
                                            candidate_cached=new.cache_hit, skill_usage=skill_usage))
            complete = baseline_complete and all(pair.candidate.complete for pair in pairs)
            # Shared baseline calls are charged once, to the first report in this batch.
            usage = sum_usage(candidate_costs + (charged if index == 0 else []))
            local = ValidationEvidence(conditions=condition_data, results=[pair for pair in pairs if pair.group == "local"])
            independent = ValidationEvidence(conditions=condition_data, results=[pair for pair in pairs if pair.group != "local"])
            local_ref = self.artifacts.put(local.model_dump_json(indent=2).encode("utf-8"), "local_validation")
            private_ref = self.artifacts.put(independent.model_dump_json(indent=2).encode("utf-8"), "validation_results")
            failures = [result.error for pair in pairs for result in (pair.baseline, pair.candidate) if result.error is not None]
            error = failures[0] if failures else None
            new_results = [(pair.group, pair.candidate) for pair in pairs]
            local_pair = pairs[0]
            local_metrics = None
            if local_pair.baseline.evaluation is not None and local_pair.candidate.evaluation is not None:
                old_score, new_score = local_pair.baseline.evaluation, local_pair.candidate.evaluation
                local_metrics = LocalValidationMetrics(baseline_score=old_score.score, candidate_score=new_score.score,
                    baseline_success=old_score.success, candidate_success=new_score.success,
                    delta_score=new_score.score - old_score.score, repaired=not old_score.success and new_score.success)
            reports.append(ValidationReport(validation_id=timestamp_id("validation"), candidate_id=candidate.candidate_id,
                baseline_version_id=stable.version_id, candidate_version_id=version.version_id,
                status="complete" if complete else "error" if error else "incomplete",
                baseline_metrics=metrics(baseline) if complete else None,
                candidate_metrics=metrics(new_results, baseline) if complete else None,
                local_result_ref=local_ref, private_results_ref=private_ref,
                candidate_validation_tokens=sum_usage(pair.candidate.usage for pair in pairs).total_tokens,
                candidate_cache_hits=sum(pair.candidate_cached for pair in pairs),
                usage=usage, error=error, local_metrics=local_metrics))
        return tuple(reports)
