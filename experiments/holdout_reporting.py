"""Post-episode initial/final holdout observations, including unchanged versions.

Opt-in batch reporting only. No diagnosis, search, acceptance or commit occurs.
Compatible validation observations are reused; missing observations are measured.
"""

from dataclasses import replace
import json
from statistics import fmean

from aer_core.cached_executor import CachedTaskExecutor
from aer_core.errors import AERError
from aer_core.schemas.paired import ScoredExecution
from aer_core.schemas.usage import ResourceUsage
from aer_core.task_executor import ScoredTaskExecutor
from aer_core.usage import sum_usage
from plugins.validation.cache import LocalValidationCache
from plugins.validation.paired import paired_seed
from .conditions import validation_conditions


def measure_holdouts(episode, initial, final, groups):
    execution = episode.execution
    prepared = execution.benchmark
    executor = CachedTaskExecutor(ScoredTaskExecutor(execution.agent, prepared.benchmark, prepared.evaluator),
        LocalValidationCache(execution.storage.run_root), validation_conditions(execution))
    observations, evidence, usages = [], [], []
    for group, tasks in zip(("similar", "regression"), groups, strict=True):
        for task in tasks:
            seed = paired_seed(episode.context.seed, episode.task.identity, task.identity)
            observed, full = {"group": group, "task_id": task.task_id, "seed": seed}, {}
            for name, snapshot in (("initial", initial), ("final", final)):
                context = replace(episode.context, stage=group + "_validation", seed=seed,
                    operation_id=episode.context.operation_id + "/holdout-report/" + name + "/" + task.task_id)
                try:
                    result = executor.execute(task, snapshot, context)
                except AERError as failure:
                    error = failure.to_info()
                    usage = error.usage or ResourceUsage(input_tokens=None, output_tokens=None, total_tokens=None,
                        model_calls=None, tool_calls=0, elapsed_seconds=0, usage_complete=False, usage_sources=["unknown"])
                    result = ScoredExecution(task=task.identity, version_id=snapshot.version_id, seed=seed,
                        execution=None, evaluation=None, usage=usage, error=error)
                usages.append(result.charged_usage)
                full[name] = result.model_dump(mode="json")
                observed[name] = {"version_id": snapshot.version_id,
                    "evaluation": result.evaluation.model_dump(mode="json") if result.evaluation else None,
                    "error": result.error.model_dump(mode="json") if result.error else None,
                    "complete": result.complete, "cache_hit": result.cache_hit,
                    "operation_id": result.execution.operation_id if result.execution else None}
            observations.append(observed)
            evidence.append({"group": group, "task_id": task.task_id, **full})
    usage = sum_usage(usages)
    ref = execution.storage.artifact_store.put(
        json.dumps({"schema_version": "aer.holdout_observations.v1",
            "current_task_id": episode.task.task_id, "observations": evidence}, ensure_ascii=False, indent=2).encode("utf-8"),
        "holdout_observations")
    report = {"status": "complete" if all(o[name]["complete"] for o in observations
               for name in ("initial", "final")) else "incomplete",
        "comparison": "episode_initial_vs_final_stable",
        "unchanged_version": initial.version_id == final.version_id,
        "observations": observations, "evidence_ref": ref.model_dump(mode="json"),
        "usage": usage.model_dump(mode="json"),
        "note": "Post-decision reporting; exact cached observations reused, including unchanged versions. Not an independent final test."}
    return report, usage


def paired_metrics(pairs, *, expected):
    """Rates keep missing observations visible; deltas use fully scored pairs."""
    old = [a for a, _ in pairs if a is not None]
    new = [b for _, b in pairs if b is not None]
    paired = [(a, b) for a, b in pairs if a is not None and b is not None]
    transitions = {"failure_to_success": 0, "success_to_failure": 0, "both_success": 0, "both_failure": 0}
    for a, b in paired:
        key = ("both_success" if b["success"] else "success_to_failure") if a["success"] else (
            "failure_to_success" if b["success"] else "both_failure")
        transitions[key] += 1
    old_successes = sum(a["success"] for a, _ in paired)
    return {"expected_count": expected, "initial_evaluated_count": len(old), "final_evaluated_count": len(new),
        "paired_count": len(paired), "missing_initial_count": expected - len(old), "missing_final_count": expected - len(new),
        "initial_success_rate_all": sum(a["success"] for a in old) / expected if expected else None,
        "final_success_rate_all": sum(b["success"] for b in new) / expected if expected else None,
        "initial_mean_score_evaluated": fmean(a["score"] for a in old) if old else None,
        "final_mean_score_evaluated": fmean(b["score"] for b in new) if new else None,
        "score_gain_paired": fmean(b["score"] - a["score"] for a, b in paired) if paired else None,
        "success_rate_gain_paired": fmean(int(b["success"]) - int(a["success"]) for a, b in paired) if paired else None,
        "transitions": transitions,
        "success_loss_rate_among_initial_successes_paired": transitions["success_to_failure"] / old_successes if old_successes else None,
        "rate_note": "All-count rates count missing observations as unsuccessful; score means exclude missing and report their counts."}


def aggregate_holdouts(entries):
    result = {}
    for group in ("similar", "regression"):
        pairs = []
        expected = sum(len(entry.get("holdouts", {}).get(group, [])) for entry in entries)
        for entry in entries:
            for row in entry.get("holdout_reporting", {}).get("observations", []):
                if row["group"] == group:
                    pairs.append(tuple(row[name]["evaluation"] if row[name]["error"] is None else None
                                       for name in ("initial", "final")))
        result[group] = paired_metrics(pairs, expected=expected)
    return result
