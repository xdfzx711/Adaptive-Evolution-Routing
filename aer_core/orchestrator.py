"""Synchronous, single-episode evolution controller over injected interfaces."""

import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from time import monotonic

from aer_core.config.models import ExperimentConfig
from aer_core.context import ContextBuilder
from aer_core.feedback import validate_diagnosis
from aer_core.contracts.diagnoser import DiagnoserPlugin
from aer_core.contracts.orchestration import CandidateValidationService, ProposalService
from aer_core.contracts.storage import ArtifactStore, ExperienceStore, StoragePlacement, VersionStore
from aer_core.contracts.validation import ValidationTaskExecutor
from aer_core.errors import AERError, CommitError, CommitFinalizationError, ContractError, ValidationError
from aer_core.identifiers import timestamp_id
from aer_core.progress import ProgressReporter, report_progress, report_record
from aer_core.schemas.assessment import TaskExecutionRecord
from aer_core.schemas.execution import ErrorInfo
from aer_core.schemas.experience import AttemptStartedEvent, CandidateEvaluatedEvent, CommitRequest, TaskFinishedEvent
from aer_core.schemas.experiment import RunResult, TaskFinishedSummary
from aer_core.schemas.paired import ScoredExecution
from aer_core.schemas.references import AgentSnapshot
from aer_core.schemas.runtime import RunContext
from aer_core.schemas.task import Task, TaskView
from aer_core.schemas.usage import ResourceUsage
from aer_core.usage import sum_usage


@dataclass
class _Episode:
    initial: AgentSnapshot
    stable: AgentSnapshot
    started: float = field(default_factory=monotonic)
    record: TaskExecutionRecord | None = None
    last_scored: ScoredExecution | None = None
    costs: list[ResourceUsage] = field(default_factory=list)
    rounds: int = 0
    stage: str = "initial_execution"
    charged: bool = False


class Orchestrator:
    def __init__(self, *, config: ExperimentConfig, executor: ValidationTaskExecutor,
                 diagnoser: DiagnoserPlugin, contexts: ContextBuilder, artifacts: ArtifactStore,
                 versions: VersionStore, experience: ExperienceStore,
                 evolution: ProposalService | None, validation: CandidateValidationService | None,
                 progress: ProgressReporter | None = None, storage_placement: StoragePlacement | None = None):
        self.config, self.executor, self.diagnoser, self.contexts = config, executor, diagnoser, contexts
        self.artifacts, self.versions, self.experience = artifacts, versions, experience
        self.evolution, self.validation = evolution, validation
        self.progress = progress
        self.storage_placement = storage_placement

    def _notify(self, stage, message):
        report_progress(self.progress, stage, message)

    def _context(self, base, stage):
        identity = timestamp_id(stage)
        return replace(base, stage=stage, operation_id=base.operation_id + "/" + identity,
            workspace_ref=base.workspace_ref + "/" + identity)

    def _save(self, value, kind):
        ref = self.artifacts.put(value.model_dump_json(indent=2).encode("utf-8"), kind)
        report_record(self.progress, kind, value, ref)
        return ref

    def _commit_outcome(self, selected, context, *, error=None):
        outcome = selected.replace(status="commit_failed" if error else "committed", committed=error is None,
            commit_error=error, final_recorded_at=datetime.now(timezone.utc))
        self.experience.append(CandidateEvaluatedEvent(event_id=selected.candidate_id + ("/commit-failed" if error else "/committed"),
            run_id=context.run_id, episode_id=context.episode_id, timestamp=outcome.final_recorded_at, payload=outcome))
        report_record(self.progress, "commit_outcome", outcome)

    def _rounds(self, context):
        return sum(isinstance(event, AttemptStartedEvent) for event in self.experience.events()
                   if event.run_id == context.run_id and event.episode_id == context.episode_id)

    def _failure(self, failure, stage):
        if isinstance(failure, AERError):
            return failure.to_info()
        return ErrorInfo(code="controller_error", stage=stage,
            message="Component failed during the task evolution loop")

    def _stop_reason(self, error, stage):
        if error.code == "budget_exceeded":
            return "budget_exhausted"
        if error.code == "storage_error":
            return "storage_error"
        if stage in {"diagnosis", "routing", "evolution", "commit"}:
            return stage + "_error"
        if stage in {"local_validation", "similar_validation", "regression_validation", "acceptance"}:
            return "validation_error"
        return "evaluation_error" if error.stage == "evaluation" else "execution_error"

    def _finish(self, task, context, state, reason, error=None):
        state.rounds = self._rounds(context)
        events = [event for event in self.experience.events()
                  if event.run_id == context.run_id and event.episode_id == context.episode_id]
        refs = [self.experience.append(event) for event in events]
        latest = state.last_scored
        evaluated = latest is not None and latest.version_id == state.stable.version_id
        result = RunResult(run_id=context.run_id, episode_id=context.episode_id, task_id=task.task_id,
            initial_version_id=state.initial.version_id, final_stable_version_id=state.stable.version_id,
            status="budget_interrupted" if reason == "budget_exhausted" else "error" if reason.endswith("_error") else "completed",
            final_evaluation_status="evaluated" if evaluated else "not_evaluated",
            final_execution=latest.execution if evaluated else None, final_evaluation=latest.evaluation if evaluated else None,
            last_evaluated_execution=latest.execution if latest else None,
            last_evaluated_evaluation=latest.evaluation if latest else None,
            evolution_rounds_used=state.rounds, stop_reason=reason, stop_stage=state.stage,
            experience_refs=refs, usage=sum_usage(state.costs).replace(elapsed_seconds=monotonic() - state.started), error=error)
        result_ref = self._save(result, "run_result")
        self.experience.append(TaskFinishedEvent(event_id=context.episode_id + "/finished", run_id=context.run_id,
            episode_id=context.episode_id, timestamp=datetime.now(timezone.utc),
            payload=TaskFinishedSummary.from_result(result, result_ref)))
        score = f"{result.final_evaluation.score:.3f}" if result.final_evaluation else "未评测"
        self._notify("结束", f"状态={result.status.value}；停止原因={reason}；进化 {state.rounds} 轮；"
            f"最终得分={score}；稳定版本{'已更新' if state.stable.version_id != state.initial.version_id else '未更新'}")
        if error:
            self._notify("异常", f"{error.stage.value} / {error.code}：{error.message}")
        usage = result.usage
        self._notify("成本", f"tokens={usage.total_tokens if usage.total_tokens is not None else '未知'}；"
            f"模型调用={usage.model_calls if usage.model_calls is not None else '未知'}；工具调用={usage.tool_calls}；耗时={usage.elapsed_seconds:.1f}s；"
            f"usage={'完整' if usage.usage_complete else '不完整'}")
        return result

    def _execute(self, task, context, state, stage):
        state.stage, state.charged = stage, False
        label = "初始执行" if stage == "initial_execution" else "任务执行"
        self._notify(label, f"获取 {task.task_id} 的执行与评分")
        operation = self._context(context, stage)
        scored = self.executor.execute(task, state.stable, operation)
        if not isinstance(scored, ScoredExecution):
            raise ContractError("Executor must return ScoredExecution", stage=stage)
        state.costs.append(scored.charged_usage)
        state.charged = True
        if scored.task != task.identity or scored.version_id != state.stable.version_id or scored.seed != operation.seed:
            raise ContractError("Task execution differs from the episode task/version/seed", stage=stage)
        self._save(scored, "controller_execution")
        if scored.error is not None:
            return scored.error
        if scored.evaluation is None or scored.execution is None:
            raise ContractError("Current task execution has no external score", stage=stage)
        self._set_task_result(task, context, state, scored)
        if not scored.complete:
            return ContractError("Task usage is incomplete; cannot reuse this observation for evolution",
                                 stage=stage, usage=scored.usage).to_info()
        return None

    def _set_task_result(self, task, context, state, scored):
        state.last_scored = scored
        # Only fixed public explanations are exposed to diagnosis and evolution.
        evaluation = scored.evaluation
        feedback = self.contexts.feedback(evaluation)
        state.record = TaskExecutionRecord(run_id=context.run_id, episode_id=context.episode_id, task=task.identity,
            execution=scored.execution, evaluation=evaluation, feedback=feedback, diagnosis=None,
            execution_usage=scored.charged_usage, execution_reused=scored.cache_hit)
        output = json.dumps(scored.execution.model_dump(mode="json")["output"], ensure_ascii=False, separators=(",", ":"))
        label = "采用验证结果" if state.stage == "commit" else "初始执行"
        source = "复用已保存结果" if scored.cache_hit else "本次执行"
        self._notify(label, f"{source}；{'成功' if evaluation.success else '未通过'}；得分={evaluation.score:.3f}；"
            f"输出={output[:300]}{'…' if len(output) > 300 else ''}")

    def _adopt_validated(self, task, context, state, scored):
        reused = scored.replace(cache_hit=True, reuse_operation_id=context.operation_id + f"/adopt/{state.rounds}")
        self._set_task_result(task, context, state, reused)
        report_record(self.progress, "committed_task_result", reused)

    def run_task(self, task: Task, public_task: TaskView, context: RunContext, *, allowed_history_task_ids=()) -> RunResult:
        if task.identity.benchmark_id != public_task.benchmark_id or task.task_id != public_task.task_id:
            raise ContractError("Public task identity differs from the requested task", stage="setup")
        if any(event.run_id == context.run_id and event.episode_id == context.episode_id for event in self.experience.events()):
            raise ContractError("Use a new episode_id; replaying a task episode is not supported", stage="setup")
        initial = self.versions.stable()
        state = _Episode(initial, initial)
        if self.storage_placement is not None:
            self.storage_placement.begin_episode(task.identity, context)
        report_record(self.progress, "episode_start", initial)
        self._notify("开始", f"任务={task.task_id}；模式={self.config.evolution.mode}；"
            f"最多进化 {self.config.limits.max_evolution_rounds} 轮；seed={context.seed}")
        try:
            error = self._execute(task, context, state, "initial_execution")
            if error:
                return self._finish(task, context, state, self._stop_reason(error, state.stage), error)
            while True:
                if state.record.evaluation.success and self.config.evolution.stop_on_success:
                    return self._finish(task, context, state, "task_succeeded")
                state.stage, state.charged = "routing", True
                if self.config.evolution.mode == "disabled":
                    return self._finish(task, context, state, "evolution_disabled")
                if state.rounds >= self.config.limits.max_evolution_rounds:
                    return self._finish(task, context, state, "max_evolution_rounds_reached")
                round_id = state.rounds + 1
                if self.config.evolution.mode == "manual" and not any(
                    item.round_id == round_id for item in self.config.routing.manual_decisions):
                    return self._finish(task, context, state, "manual_plan_exhausted")
                if self.evolution is None or self.validation is None:
                    raise ContractError("Evolution services are missing", stage="routing")
                feasible = self.evolution.feasible_targets(state.stable)
                if not feasible:
                    return self._finish(task, context, state, "no_feasible_targets")
                if self.storage_placement is not None:
                    self.storage_placement.begin_round(round_id)
                report_record(self.progress, "round_start", round_id)
                self._notify("进化", f"第 {round_id}/{self.config.limits.max_evolution_rounds} 轮；可行目标={', '.join(feasible)}")
                diagnosis_key = getattr(self.diagnoser, "evidence_key", None)
                if state.record.diagnosis is not None and callable(diagnosis_key):
                    diagnostic_view = self.contexts.build(public_task, state.record, state.stable, self._context(context, "diagnosis"),
                        round_id=round_id, feasible_targets=feasible, task_mode=self.config.state.task_mode,
                        allowed_history_task_ids=allowed_history_task_ids)
                    if state.record.diagnosis.evidence_key != diagnosis_key(diagnostic_view.task, diagnostic_view.execution, diagnostic_view.feedback):
                        state.record = state.record.replace(diagnosis=None)
                if state.record.diagnosis is None:
                    state.stage, state.charged = "diagnosis", False
                    diagnostic_ctx = self._context(context, "diagnosis")
                    view = self.contexts.build(public_task, state.record, state.stable, diagnostic_ctx,
                        round_id=round_id, feasible_targets=feasible, task_mode=self.config.state.task_mode,
                        allowed_history_task_ids=allowed_history_task_ids)
                    diagnosis = validate_diagnosis(self.diagnoser.diagnose(view.task, view.execution, view.feedback, diagnostic_ctx), view.execution, view.feedback)
                    state.costs.append(diagnosis.usage or ResourceUsage.zero())
                    state.charged = True
                    state.record = state.record.replace(diagnosis=diagnosis)
                    report_record(self.progress, "diagnosis", diagnosis)
                    self._notify("诊断", f"{diagnosis.reason_code}；{diagnosis.explanation}")
                state.stage, state.charged = "evolution", False
                self._notify("生成", "正在路由并生成候选")
                proposal = self.evolution.propose(state.record, round_id=round_id)
                state.rounds = self._rounds(context)
                if proposal.status == "skipped":
                    return self._finish(task, context, state, proposal.stop_reason.value)
                state.costs.append(proposal.attempt.usage)
                state.charged = True
                if ((proposal.run_id, proposal.episode_id, proposal.task_id, proposal.stable_version_id, proposal.round_id) !=
                    (context.run_id, context.episode_id, task.task_id, state.stable.version_id, round_id) or state.rounds != round_id):
                    raise ContractError("Proposal scope or persisted attempt count differs from episode", stage="evolution")
                self._save(proposal, "evolution_proposal")
                self._notify("生成", f"目标={proposal.decision.target if proposal.decision else '未选择'}；"
                    f"Operator={proposal.operator_id or '未选择'}；候选={len(proposal.response.candidates)}；"
                    f"tokens={proposal.attempt.usage.total_tokens}")
                if proposal.response.status in {"error", "budget_interrupted"}:
                    error = proposal.response.error or ValidationError("Evolution was interrupted", stage="evolution").to_info()
                    state.stage = "routing" if error.stage == "routing" else "evolution"
                    reason = "budget_exhausted" if proposal.response.status == "budget_interrupted" else self._stop_reason(error, state.stage)
                    return self._finish(task, context, state, reason, error)
                if not proposal.response.candidates:
                    continue
                state.stage, state.charged = "local_validation", False
                self._notify("验证", "开始配对验证：当前任务、同类任务、回归任务；相同条件的旧版结果可复用")
                batch = self.validation.validate(proposal)
                state.costs.append(batch.usage)
                state.charged = True
                if ((batch.run_id, batch.episode_id, batch.attempt_id, batch.stable_version_id) !=
                    (context.run_id, context.episode_id, proposal.attempt.attempt_id, state.stable.version_id) or
                    {item.candidate_id for item in batch.outcomes} != {item.candidate_id for item in proposal.response.candidates}):
                    raise ValidationError("Validation outcomes differ from episode proposal", stage="local_validation")
                self._save(batch, "validation_batch")
                reports = {report.candidate_id: report for report in batch.reports}
                by_id = {item.candidate_id: index for index, item in enumerate(proposal.response.candidates, 1)}
                for outcome in batch.outcomes:
                    report = reports.get(outcome.candidate_id)
                    scores = ""
                    if report and report.baseline_metrics and report.candidate_metrics:
                        old, new = report.baseline_metrics, report.candidate_metrics
                        scores = (f"；验证分={old.score:.3f}→{new.score:.3f}"
                            f"；同类变化={new.similar_score - old.similar_score:+.3f}"
                            f"；回归变化={new.regression_score - old.regression_score:+.3f}")
                    if report and report.local_metrics:
                        local = report.local_metrics
                        scores += f"；当前任务={local.baseline_score:.3f}→{local.candidate_score:.3f}"
                    acceptance = outcome.acceptance
                    self._notify("候选结果", f"候选 {by_id[outcome.candidate_id]}：{outcome.status.value}"
                        f"；原因={acceptance.reason if acceptance else '未完成验证'}{scores}")
                if any(report.status != "complete" for report in batch.reports):
                    error = next((report.error for report in batch.reports if report.error), None) or ValidationError(
                        "Candidate validation is incomplete, including its reported usage", stage="local_validation").to_info()
                    return self._finish(task, context, state, "validation_error", error)
                if batch.selected_candidate_id is None:
                    continue
                selected = next(item for item in batch.outcomes if item.candidate_id == batch.selected_candidate_id)
                selected_result = batch.current_task_results.get(selected.candidate_id)
                if (selected_result is None or not selected_result.complete or selected_result.task != task.identity or
                    selected_result.version_id != selected.candidate_version_id or selected_result.seed != context.seed):
                    raise ValidationError("Selected candidate requires complete, matching current-task evidence", stage="local_validation")
                state.stage, state.charged = "commit", True
                self._notify("提交", f"提交候选 {by_id[selected.candidate_id]} 为稳定版本")
                snapshot = self.versions.load(selected.candidate_version_id)
                request = CommitRequest(run_id=context.run_id, episode_id=context.episode_id,
                    attempt_id=batch.attempt_id, candidate_id=selected.candidate_id,
                    expected_stable_version_id=state.stable.version_id, validated_snapshot=snapshot,
                    validation_ref=selected.validation_ref, acceptance=selected.acceptance)
                try:
                    committed = self.versions.commit(request)
                except CommitFinalizationError as interrupted:
                    # Recovery finalizes the already published commit; it does not regenerate or revalidate.
                    state.stable = snapshot
                    try:
                        recovered = self.versions.stable()
                    except CommitFinalizationError:
                        # Publication is durable even if receipt recovery remains unavailable.
                        self._commit_outcome(selected, context)
                        self._adopt_validated(task, context, state, selected_result)
                        raise
                    if recovered != snapshot:
                        raise CommitError("Recovered stable version differs from selected candidate", stage="commit")
                    committed = interrupted.committed_record
                except CommitError as failure:
                    self._commit_outcome(selected, context, error=failure.to_info())
                    raise
                if committed.committed_version_id != snapshot.version_id:
                    raise CommitError("Commit receipt differs from the selected candidate", stage="commit")
                state.stable = snapshot
                self._commit_outcome(selected, context)
                self._notify("提交", "稳定版本已更新，采用候选在当前任务上的验证结果")
                self._adopt_validated(task, context, state, selected_result)
        except Exception as failure:
            error = self._failure(failure, state.stage)
            if not state.charged:
                state.costs.append(error.usage or ResourceUsage(input_tokens=None, output_tokens=None, total_tokens=None,
                    model_calls=0, tool_calls=0, elapsed_seconds=0, usage_complete=False, usage_sources=["unknown"]))
            return self._finish(task, context, state, self._stop_reason(error, state.stage), error)
