from importlib.metadata import PackageNotFoundError, version
from time import monotonic

from aer_core.errors import AERError, EvolutionError, PluginConfigurationError
from aer_core.progress import report_progress
from aer_core.schemas.evolution import EvolutionCandidate, EvolutionCapabilities, EvolutionResponse
from aer_core.schemas.optimization import OptimizationRecord
from aer_core.usage import sum_usage
from plugins.evolution.prompt import _change_summary
from .adapter import AERGEPAAdapter
from .archive import PromptArchive
from .recording import QuietLogger, SearchLimit, SearchRecord, now
from .reflection import ProviderReflection


GEPA_VERSION = "0.1.4"
COMPATIBLE_AGENTS = ("react_agent", "single_call_agent", "codex_cli_agent")


def require_gepa():
    try:
        installed = version("gepa")
    except PackageNotFoundError:
        installed = None
    if installed != GEPA_VERSION:
        raise PluginConfigurationError("GEPA plugin requires gepa==" + GEPA_VERSION + "; install the project's [gepa] extra",
                                       stage="setup", plugin_id="gepa_prompt_evolver")


class GEPAPromptEvolver:
    plugin_id = "gepa_prompt_evolver"

    def __init__(self, config, artifacts, reflection_model, trials, progress=None):
        require_gepa()
        self.config, self.artifacts, self.reflection_model, self.trials = config, artifacts, reflection_model, trials
        self.progress = progress
        self.archive = PromptArchive()

    def capabilities(self):
        return EvolutionCapabilities(supported_targets=["prompt"], compatible_agents=COMPATIBLE_AGENTS,
            required_artifact_kinds=["prompt"], supports_multiple_candidates=False, reports_usage=True,
            target_descriptions={"prompt": "Optimize reusable task instructions through GEPA reflection and isolated "
                "trial search. Returns one prompt candidate; does not change execution model, reasoning or tools."})

    def evolve(self, request):
        import gepa
        stable = request.context.stable_snapshot
        old = self.artifacts.get(stable.artifacts["prompt"]).decode("utf-8")
        record = SearchRecord(old, self.config, seen_prompts=self.archive.for_request(request), round_id=request.round_id)
        adapter = AERGEPAAdapter(self.trials, request, record)
        error, result = None, None
        training, selection = (), ()
        status, reason = "empty", "no_improvement"
        candidates = []
        try:
            if request.decision.target != "prompt" or stable.agent_plugin_id not in COMPATIBLE_AGENTS or not old.strip():
                raise EvolutionError("GEPA requires a supported Prompt target and non-empty seed", stage="evolution")
            training = self.trials.tasks(adapter.current_task, tuple(self.config.train_task_ids) if self.config.train_task_ids else None)
            selection = self.trials.tasks(adapter.current_task, tuple(self.config.selection_task_ids)
                if self.config.selection_task_ids else tuple(item.task_id for item in training))
            report_progress(self.progress, "内部搜索", f"GEPA 开始；优化任务={len(training)}；内部选择任务={len(selection)}；"
                f"每个提示词/任务采样={self.config.samples_per_evaluation} 次；最多实际评测={self.config.max_metric_calls}")
            adapter.progress = self.progress
            try:
                result = gepa.optimize(seed_candidate={"prompt": old}, trainset=list(training), valset=list(selection),
                    adapter=adapter, reflection_lm=ProviderReflection(self.reflection_model, request.run_context, record, self.progress),
                    candidate_selection_strategy="pareto", reflection_minibatch_size=self.config.reflection_minibatch_size,
                    skip_perfect_score=self.config.skip_perfect_score, use_merge=self.config.use_merge,
                    max_merge_invocations=self.config.max_merge_invocations, merge_val_overlap_floor=self.config.merge_val_overlap_floor,
                    # GEPA counts task examples (including valset cache hits),
                    # not our repeated executions. SearchRecord enforces the
                    # configured limit against actual samples instead.
                    max_metric_calls=None, stop_callbacks=[record.stop], logger=QuietLogger(record),
                    # The adapter owns the sampled cache, including trajectories;
                    # GEPA's score/output cache cannot supply reflection traces.
                    callbacks=[record], run_dir=None, cache_evaluation=False, display_progress_bar=False,
                    seed=request.run_context.seed, raise_on_exception=True)
                record.apply_result(result)
            except SearchLimit as limit:
                record.stop_reason = str(limit)
            if record.failure is not None:
                raise EvolutionError("GEPA internal operation failed: " + record.failure.code, stage="evolution")
            scored = [candidate for candidate in record.candidates if candidate.score is not None]
            if scored:
                best = max(scored, key=lambda candidate: (candidate.score, -candidate.candidate))
                record.candidates[best.candidate - 1] = best.replace(selected=True)
                if best.prompt != old:
                    ref = self.artifacts.put(best.prompt.encode("utf-8"), "prompt")
                    candidates = [EvolutionCandidate(candidate_id=f"{request.attempt_id}/candidate/1",
                        parent_version_id=stable.version_id, target="prompt", artifact_updates={"prompt": ref},
                        metadata={"operator": self.plugin_id, "rationale": "GEPA 内部选择集最佳提示词；仍需 AER 外层验证。",
                            "change_summary": _change_summary(old, best.prompt), "round_id": request.round_id,
                            "internal_candidate": best.candidate, "internal_score": best.score})]
                    status, reason = "completed", "optimized_prompt"
                else:
                    reason = "duplicate_prompt" if record.duplicate_exhausted else "best_is_seed"
            else:
                reason = "no_complete_selection_result"
        except Exception as failure:
            status, reason = "error", "optimization_error"
            error = failure.to_info() if isinstance(failure, AERError) else EvolutionError(
                "GEPA internal search failed (" + type(failure).__name__ + ")", stage="evolution", plugin_id=self.plugin_id).to_info()
        usage = sum_usage([trial.observation.usage for trial in record.trials] + [call.usage for call in record.reflections])
        usage = usage.replace(elapsed_seconds=monotonic() - record.start)
        if error:
            error = error.replace(stage="evolution", usage=usage, operation_id=request.run_context.operation_id)
        runtime_info = getattr(self.reflection_model, "runtime_info", None)
        receipt = OptimizationRecord(optimizer=self.plugin_id, engine_version=GEPA_VERSION,
            started_at=record.started_at, finished_at=now(), status=status, reason=reason, stop_reason=record.stop_reason,
            config={**self.config.model_dump(mode="json"), "training_tasks": [item.model_dump(mode="json") for item in training],
                    "selection_tasks": [item.model_dump(mode="json") for item in selection],
                    "selection_reuses_training": training == selection,
                    "evaluation_reuse_scope": "single_search",
                    "score_aggregation": "mean",
                    "reflection_runtime": runtime_info() if callable(runtime_info) else None},
            trials=record.trials, reflections=record.reflections, candidates=record.candidates, decisions=record.decisions,
            usage=usage, error=error)
        try:
            ref = self.artifacts.put(receipt.model_dump_json(indent=2).encode("utf-8"), "evolution_optimization")
        except Exception as failure:
            raise EvolutionError("GEPA optimization receipt could not be saved", stage="evolution",
                                 plugin_id=self.plugin_id, usage=usage) from failure
        report_progress(self.progress, "内部搜索", f"GEPA 结束；状态={status}；原因={reason}；评测={len(record.trials)}；反思={len(record.reflections)}")
        return EvolutionResponse(status=status, candidates=candidates, usage=usage, error=error, record_ref=ref)
