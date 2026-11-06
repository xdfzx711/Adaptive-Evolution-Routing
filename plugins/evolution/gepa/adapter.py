from copy import deepcopy
from aer_core.diagnostic_evidence import diagnosis_payload, history_payload
from dataclasses import replace
from statistics import fmean, pstdev

from aer_core.errors import AERError, ContractError, EvolutionError
from aer_core.progress import report_progress
from aer_core.schemas.optimization import OptimizationTrial, TrialObservation
from aer_core.schemas.task import TaskIdentity, TaskView
from aer_core.schemas.usage import ResourceUsage
from .recording import now
from .archive import prompt_key
from .recording import SearchLimit


class AERGEPAAdapter:
    propose_new_texts = None

    def __init__(self, service, request, record):
        self.service, self.request, self.record = service, request, record
        self.progress = None
        self.current_task = TaskIdentity(task_id=request.context.task.task_id, benchmark_id=request.context.task.benchmark_id)
        # One adapter belongs to one search. Cache public observations as well
        # as scores, so a later reflection can reuse a trace-free valset call.
        self.evaluations = {}

    def _key(self, task, prompt):
        return (prompt, self.current_task.model_dump_json(), task.model_dump_json(),
                self.request.context.stable_snapshot.model_dump_json(),
                replace(self.request.run_context, budget=None).to_record().model_dump_json(),
                self.record.config.samples_per_evaluation, self.record.config.trial_timeout_seconds)

    def evaluate(self, batch, candidate, capture_traces=False):
        from gepa.core.adapter import EvaluationBatch
        if set(candidate) != {"prompt"} or not isinstance(candidate["prompt"], str) or not candidate["prompt"].strip():
            raise ContractError("GEPA candidates must contain only a non-empty prompt", stage="evolution")
        # Covers merge proposals too. Parent/seed re-evaluation in GEPA remains
        # valid; block only a new proposal already seen in an earlier round.
        first = self.record.duplicate_round(candidate["prompt"])
        if (first is not None and first < self.record.round_id
                and prompt_key(candidate["prompt"]) != prompt_key(self.record.candidates[0].prompt)):
            self.record.duplicate_exhausted = True
            self.record.stop_reason = "duplicate_prompt"
            self.record.decisions.append({"event": "duplicate_prompt", "first_seen_round": first,
                "iteration": self.record.iterations, "action": "skip_trial",
                "reason": f"与第 {first} 轮提示词重复；跳过试跑"})
            raise SearchLimit("duplicate_prompt")
        if self.record.failure is not None:
            raise EvolutionError("GEPA internal evaluation has already failed", stage="evolution")
        batch = list(batch)
        keys = [self._key(task, candidate["prompt"]) for task in batch]
        missing = set(keys).difference(self.evaluations)
        # Reserve whole groups before starting work: never rank a candidate
        # using fewer samples just because the budget is almost exhausted.
        if missing:
            self.record.check_limit(len(missing) * self.record.config.samples_per_evaluation)
        outputs, scores, trajectories = [], [], []
        before = len(self.record.trials)
        number = self.record.number(candidate["prompt"])
        for task, key in zip(batch, keys):
            if key in self.evaluations:
                group = self.evaluations[key]
                self.record.decisions.append({"event": "evaluation_reused", "iteration": self.record.iterations,
                    "candidate": number, "task_id": task.task_id, "trials": group["trials"], "score": group["mean_score"],
                    "reason": f"复用 {len(group['samples'])} 次采样的平均分与轨迹，不重复执行"})
                report_progress(self.progress, "内部复用", f"候选 {number} / {task.task_id}：平均分={group['mean_score']:.3f}；不重复执行")
            else:
                samples, trial_numbers, seeds = [], [], []
                count = self.record.config.samples_per_evaluation
                for index in range(count):
                    # Every prompt uses the same seed schedule when supported
                    # by the Agent. Fresh sessions are still required either way.
                    seed = self.request.run_context.seed + index
                    observed = self._sample(task, candidate["prompt"], number, index + 1, count, seed)
                    samples.append(observed.model_dump(mode="json"))
                    trial_numbers.append(len(self.record.trials))
                    seeds.append(seed)
                values = [item["feedback"]["score"] for item in samples]
                group = {"samples": samples, "trials": trial_numbers, "sample_seeds": seeds,
                         "mean_score": fmean(values), "score_stddev": pstdev(values)}
                self.evaluations[key] = group
                self.record.decisions.append({"event": "sampled_evaluation", "iteration": self.record.iterations,
                    "candidate": number, "task_id": task.task_id, "sample_count": count,
                    "sample_scores": values, "sample_seeds": seeds, "trials": trial_numbers,
                    "score": group["mean_score"], "score_stddev": group["score_stddev"],
                    "reason": f"完成 {count} 次采样；平均分={group['mean_score']:.3f}；标准差={group['score_stddev']:.3f}"})
                report_progress(self.progress, "内部汇总", f"候选 {number} / {task.task_id}：平均分={group['mean_score']:.3f}；标准差={group['score_stddev']:.3f}")
            outputs.append({"samples": [deepcopy(item["execution"]["output"]) for item in group["samples"]]})
            scores.append(group["mean_score"])
            trajectories.append(deepcopy(group))
        return EvaluationBatch(outputs=outputs, scores=scores, trajectories=trajectories if capture_traces else None,
                               num_metric_calls=len(self.record.trials) - before)

    def _sample(self, task, prompt, number, sample, count, seed):
        self.record.check_limit()
        context = replace(self.request.run_context, budget=None, seed=seed,
            model_timeout_seconds=min(self.request.run_context.model_timeout_seconds,
                self.record.config.trial_timeout_seconds, self.record.remaining_seconds()),
            execution_timeout_seconds=min(self.record.config.trial_timeout_seconds, self.record.remaining_seconds()))
        started_at = now()
        report_progress(self.progress, "内部试跑", f"候选 {number} / {task.task_id}：采样 {sample}/{count} 执行中")
        try:
            observed = self.service.evaluate(self.current_task, task, self.request.context.stable_snapshot, prompt, context)
            if not isinstance(observed, TrialObservation):
                raise ContractError("CandidateTrialService must return TrialObservation", stage="evolution")
        except Exception as failure:
            error = failure.to_info() if isinstance(failure, AERError) else EvolutionError("Internal trial failed", stage="evolution").to_info()
            observed = TrialObservation(task=TaskView(task_id=task.task_id, benchmark_id=task.benchmark_id,
                input={}, public_metadata={}), execution=None, feedback=None, error=error.replace(evidence_ref=None),
                usage=error.usage or ResourceUsage(input_tokens=None, output_tokens=None, total_tokens=None,
                    model_calls=None, tool_calls=0, elapsed_seconds=0, usage_complete=False, usage_sources=["unknown"]))
        self.record.trials.append(OptimizationTrial(trial=len(self.record.trials) + 1, candidate=number,
            started_at=started_at, finished_at=now(), observation=observed))
        if observed.error or observed.feedback is None or observed.execution is None or not observed.usage.usage_complete:
            self.record.failure = observed.error or EvolutionError("Internal trial has incomplete feedback/usage", stage="evolution").to_info()
            raise EvolutionError("GEPA internal execution/scoring is incomplete", stage="evolution", usage=observed.usage)
        report_progress(self.progress, "内部试跑", f"候选 {number} / {task.task_id}：采样 {sample}/{count} 得分={observed.feedback.score:.3f}")
        return observed

    def make_reflective_dataset(self, candidate, eval_batch, components_to_update):
        if list(components_to_update) != ["prompt"] or eval_batch.trajectories is None:
            raise ContractError("GEPA reflection requires prompt trajectories", stage="evolution")
        return {"prompt": [{"Inputs": item["task"], "Generated Outputs": item["execution"]["output"],
            "Feedback": item["feedback"], "Execution Evidence": item["execution"]["trajectory"],
            "Execution Environment": {key: value for key, value in item["execution"]["state_summary"].items()
                                      if key in {"execution_platform", "execution_shell"}},
            "Outer Baseline Diagnosis": diagnosis_payload(self.request.context.diagnosis) if self.request.context.diagnosis
                and item["task"]["task_id"] == self.current_task.task_id else None,
            "Outer Baseline Evidence": {"task": self.request.context.task.model_dump(mode="json"),
                "execution": self.request.context.execution.model_dump(mode="json"),
                "evaluation": self.request.context.feedback.model_dump(mode="json"),
                "reference_scope": "Outer Baseline Diagnosis references this evidence, not the internal trial."}
                if self.request.context.diagnosis and item["task"]["task_id"] == self.current_task.task_id else None,
            "Previous Evolution Attempts": history_payload(self.request.context.history),
            "Repeated Evaluation": {"sample": index + 1, "sample_count": len(group["samples"]),
                "seed": group["sample_seeds"][index], "mean_score": group["mean_score"],
                "score_stddev": group["score_stddev"]}}
            for group in eval_batch.trajectories for index, item in enumerate(group["samples"])]}
