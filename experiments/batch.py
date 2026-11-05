"""Benchmark-independent episodes with incremental audit summaries."""

import json
from pathlib import Path
from statistics import fmean
from time import monotonic

from aer_core.errors import AERError
from aer_core.contracts.benchmark import PredictionExportProvider
from aer_core.usage import sum_usage
from aer_core.schemas.usage import ResourceUsage
from experiments.configuration import load_experiment_config
from experiments.orchestration import prepare_orchestrator
from experiments.holdout_reporting import aggregate_holdouts, measure_holdouts, paired_metrics
from experiments.tool_construction_report import (
    aggregate_construction, construction_summary, tool_library, validation_tool_summary,
)
from plugins.storage._files import FileRoot, readable_json
from plugins.storage.layout import episode_directory


def aggregate(entries):
    evaluated = [entry for entry in entries if entry.get("final_evaluation") is not None]
    paired = [entry for entry in evaluated if entry.get("initial_evaluation") is not None]
    metrics = set.intersection(*(set(entry["final_evaluation"]["metrics"]) for entry in evaluated)) if evaluated else set()
    return {"finished_tasks": len(entries), "evaluated_tasks": len(evaluated),
        "error_tasks": sum(entry["status"] != "completed" for entry in entries),
        "mean_score_evaluated": fmean(entry["final_evaluation"]["score"] for entry in evaluated) if evaluated else None,
        "success_rate_evaluated": fmean(entry["final_evaluation"]["success"] for entry in evaluated) if evaluated else None,
        "paired_initial_final_tasks": len(paired),
        "mean_initial_score_paired": fmean(entry["initial_evaluation"]["score"] for entry in paired) if paired else None,
        "mean_score_gain_paired": fmean(entry["final_evaluation"]["score"] - entry["initial_evaluation"]["score"]
                                        for entry in paired) if paired else None,
        "mean_native_metrics_evaluated": {key: fmean(entry["final_evaluation"]["metrics"][key] for entry in evaluated)
                                          for key in sorted(metrics)},
        "current_task_initial_vs_final": paired_metrics([
            (entry.get("initial_evaluation"), entry.get("final_evaluation")) for entry in entries], expected=len(entries)),
        "note": "Metrics use evaluated final states only; error/missing counts are reported separately."}


def _run_task(startup, task, *, console, report_holdouts):
    benchmark = startup.execution.benchmark
    config = benchmark.loaded_config.config
    root = startup.execution.storage.run_root
    usages = []
    similar, regression = benchmark.validation_groups(task) if config.evolution.mode != "disabled" or report_holdouts else ((), ())
    entry = {"task_id": task.task_id, "holdouts": {"similar": [t.task_id for t in similar],
              "regression": [t.task_id for t in regression]}}
    try:
        episode = startup.prepare_task(task.task_id)
        initial = episode.execution.storage.version_store.stable()
        artifacts = episode.execution.storage.artifact_store
        entry["initial_prompt"] = artifacts.get(initial.artifacts["prompt"]).decode("utf-8")
        if "tool" in initial.artifacts:
            entry["initial_tool_library"] = tool_library(initial, artifacts)
        result = episode.run()
        saved = startup.save_record(result)
        usages.append(result.usage)
        directory = root / episode_directory(task.task_id, result.episode_id)
        rounds = []
        for record_path in sorted(directory.glob("round-*/record.json")):
            record = json.loads(record_path.read_text(encoding="utf-8"))
            if "initial_evaluation" not in entry:
                entry["initial_evaluation"] = record.get("evaluation_before_evolution") or (
                    record.get("execution_before_evolution") or {}).get("evaluation")
            optimization = record.get("generation", {}).get("internal_optimization") or {}
            rounds.append({"round": record["round"], "record": str(record_path.relative_to(root)),
                "internal_trials": len(optimization.get("trials", [])),
                "internal_candidates": optimization.get("candidates", []),
                "internal_status": optimization.get("status"), "internal_stop_reason": optimization.get("stop_reason"),
                "candidates": [{key: candidate.get(key) for key in
                    ("candidate", "target", "status", "committed", "acceptance", "components")}
                    | {"validation": {key: (candidate.get("validation") or {}).get(key) for key in
                        ("status", "baseline_metrics", "candidate_metrics", "current_task_metrics", "usage")}
                        | {"tool_usage_analysis": validation_tool_summary(candidate.get("validation") or {})}}
                    for candidate in record.get("candidates", [])]})
            construction = construction_summary(record)
            if construction is not None:
                rounds[-1]["tool_construction"] = construction
        final = episode.execution.storage.version_store.stable()
        entry.update(status=result.status.value, stop_reason=result.stop_reason.value,
            initial_version_id=result.initial_version_id, final_stable_version_id=result.final_stable_version_id,
            final_prompt=artifacts.get(final.artifacts["prompt"]).decode("utf-8"),
            final_evaluation=result.final_evaluation.model_dump(mode="json") if result.final_evaluation else None,
            final_output=result.final_execution.model_dump(mode="json")["output"] if result.final_execution else None,
            error=result.error.model_dump(mode="json") if result.error else None,
            rounds=rounds, usage=result.usage.model_dump(mode="json"), result_ref=saved["record_ref"])
        if "tool" in final.artifacts:
            from aer_core.tool_usage import tool_usage_summary
            entry["final_tool_library"] = tool_library(final, artifacts)
            entry["final_tool_usage"] = (tool_usage_summary(result.final_execution.model_dump(mode="json")["state_summary"])
                                         if result.final_execution else None)
        if result.evolution_rounds_used == 0:
            entry["initial_evaluation"] = entry["final_evaluation"]
        if report_holdouts:
            if console:
                print(f"[留出报告] {task.task_id}: initial/final stable，复用已有评分并补齐缺失执行", flush=True)
            try:
                report, extra_usage = measure_holdouts(episode, initial, final, (similar, regression))
            except AERError as failure:
                info = failure.to_info()
                extra_usage = info.usage or ResourceUsage(input_tokens=None, output_tokens=None, total_tokens=None,
                    model_calls=None, tool_calls=0, elapsed_seconds=0, usage_complete=False, usage_sources=["unknown"])
                report = {"status": "error", "observations": [], "error": info.model_dump(mode="json"),
                          "usage": extra_usage.model_dump(mode="json")}
            entry["holdout_reporting"] = report
            usages.append(extra_usage)
            entry["usage"] = sum_usage((result.usage, extra_usage)).model_dump(mode="json")
    except AERError as failure:
        info = failure.to_info()
        entry.update(status="error", final_evaluation=None, error=info.model_dump(mode="json"))
        if info.usage is not None:
            usages.append(info.usage)
        else:
            usages.append(ResourceUsage(input_tokens=None, output_tokens=None, total_tokens=None,
                model_calls=None, tool_calls=0, elapsed_seconds=0, usage_complete=False, usage_sources=["unknown"]))
    entry["usage"] = sum_usage(usages).model_dump(mode="json")
    return entry


def run_benchmark(path, *, run_id=None, limit=None, console=False, report_holdouts=False, workers=1):
    started = monotonic()
    loaded = load_experiment_config(path)
    if limit is not None and (type(limit) is not int or limit < 1):
        raise ValueError("limit must be a positive integer")
    if type(workers) is not int or workers < 1:
        raise ValueError("workers must be a positive integer")
    if workers > 1 and loaded.config.state.task_mode != "independent":
        raise ValueError("parallel workers require independent task_mode")
    # A fresh batch never silently reruns or appends to an old experiment.
    if run_id is not None and (Path(loaded.config.storage.root) / run_id).exists():
        raise ValueError("run-benchmark requires a new run_id")
    startup = prepare_orchestrator(loaded, run_id=run_id, console=console)
    benchmark = startup.execution.benchmark
    config = benchmark.loaded_config.config
    tasks = benchmark.execution_tasks[:limit] if limit is not None else benchmark.execution_tasks
    root = startup.execution.storage.run_root
    files = FileRoot(root)
    entries, usages = [], []
    summary = {"schema_version": "aer.baseline_summary.v1", "status": "running", "run_root": str(root),
        "benchmark_id": benchmark.benchmark.index().benchmark_id,
        "dataset_version": benchmark.benchmark.dataset_version, "scorer_version": benchmark.evaluator.scorer_version,
        "score_policy": benchmark.evaluator.score_policy, "router": config.routing.plugin,
        "fixed_target": config.routing.fixed_target, "operators": dict(config.targets.default_operators),
        "task_mode": config.state.task_mode, "acceptance": config.validation.model_dump(mode="json"),
        "requested_tasks": [task.task_id for task in tasks], "tasks": entries, "total_token_budget_enforced": False,
        "report_holdouts": report_holdouts,
        "workers": workers, "effective_workers": min(workers, len(tasks)),
        "evolution": config.evolution.model_dump(mode="json"),
        "limits": config.limits.model_dump(mode="json")}
    # A separate immutable protocol makes this run's acceptance/round settings reusable and auditable.
    protocol = {key: summary[key] for key in (
        "benchmark_id", "dataset_version", "scorer_version", "score_policy", "router", "fixed_target",
        "operators", "task_mode", "acceptance", "evolution", "limits", "workers", "effective_workers",
        "report_holdouts", "total_token_budget_enforced")}
    protocol.update(schema_version="aer.baseline_protocol.v1",
                    operator_configs=config.targets.model_dump(mode="json")["operator_configs"])
    with files.locked():
        files.replace("baseline_protocol.json", readable_json(protocol))

    def save():
        summary["wall_elapsed_seconds"] = monotonic() - started
        summary["aggregate"] = aggregate(entries)
        summary["aggregate"]["requested_tasks"] = len(tasks)
        summary["aggregate"]["pending_tasks"] = len(tasks) - len(entries)
        construction = aggregate_construction(entries)
        if construction is not None:
            summary["aggregate"]["tool_construction"] = construction
        if report_holdouts:
            summary["aggregate"]["holdouts_initial_vs_final"] = aggregate_holdouts(entries)
        summary["usage"] = sum_usage(usages).model_dump(mode="json")
        with files.locked():
            files.replace("baseline_summary.json", readable_json(summary))

    save()
    if workers > 1 and tasks:
        from experiments.parallel_batch import run_parallel_tasks
        order = {task.task_id: index for index, task in enumerate(tasks)}
        by_id = {task.task_id: task for task in tasks}
        for entry in run_parallel_tasks(loaded, tasks, root, workers=workers,
                                        console=console, report_holdouts=report_holdouts):
            if "holdouts" not in entry:
                # Worker setup/crash failures still belong in the assigned holdout denominator.
                groups = benchmark.validation_groups(by_id[entry["task_id"]]) if (
                    config.evolution.mode != "disabled" or report_holdouts) else ((), ())
                entry["holdouts"] = {name: [task.task_id for task in group]
                                     for name, group in zip(("similar", "regression"), groups, strict=True)}
            entries.append(entry)
            entries.sort(key=lambda entry: order[entry["task_id"]])
            usages.append(ResourceUsage.model_validate(entry["usage"]))
            save()
    else:
        for task in tasks:
            entry = _run_task(startup, task, console=console, report_holdouts=report_holdouts)
            usages.append(ResourceUsage.model_validate(entry["usage"]))
            entries.append(entry)
            save()
            # Failed continual episodes may have incomplete state; don't propagate it.
            if entry["status"] != "completed" and config.state.task_mode == "continual":
                break
    summary["status"] = "completed" if all(entry["status"] == "completed" and
        (not report_holdouts or entry.get("holdout_reporting", {}).get("status") == "complete")
        for entry in entries) else "completed_with_errors"
    if isinstance(benchmark.benchmark, PredictionExportProvider):
        predictions = benchmark.benchmark.export_predictions({entry["task_id"]: entry["final_output"]
            for entry in entries if entry.get("final_output") is not None})
        with files.locked():
            files.replace("predictions.json", readable_json(predictions))
        summary["predictions_path"] = str(root / "predictions.json")
    save()
    return summary


def cli_run(args):
    """Separate command handling keeps legacy CLI outputs and behavior unchanged."""
    from experiments.benchmark import prepare_benchmark
    from experiments.environment import env_file_paths

    loaded = load_experiment_config(args.config, overrides=args.overrides)
    if args.output:
        prepared = prepare_benchmark(loaded)
        protected = {loaded.source_path, *env_file_paths(loaded), *prepared.benchmark.source_paths}
        if loaded.benchmark_index_path:
            protected.add(loaded.benchmark_index_path)
        if loaded.config.agent.initial_snapshot_ref:
            protected.add(Path(loaded.config.agent.initial_snapshot_ref))
        target = args.output.resolve()
        if target in protected or target.is_relative_to(Path(loaded.config.storage.root).resolve()) or target.exists():
            raise ValueError("output must be a new file outside inputs and managed storage")
        if not target.parent.is_dir():
            raise ValueError("output parent directory must exist")
    summary = run_benchmark(loaded, run_id=args.run_id, limit=args.limit, console=not args.quiet,
                            report_holdouts=args.report_holdouts, workers=args.workers)
    if args.output:
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print(f"Baseline summary: {Path(summary['run_root']) / 'baseline_summary.json'}")
    return 0 if summary["status"] == "completed" else 2
