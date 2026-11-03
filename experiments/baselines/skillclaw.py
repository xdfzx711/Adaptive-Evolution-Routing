"""AER execution/scoring with native SkillClaw cross-task learning."""

import argparse
from hashlib import sha256
import json
from pathlib import Path

from aer_core.identifiers import timestamp_id
from aer_core.schemas.runtime import RunContext
from aer_core.task_executor import ScoredTaskExecutor
from experiments.configuration import load_experiment_config
from experiments.execution import prepare_execution
from plugins.agents.codex import CodexCliAgent
from plugins.evolution.skillclaw.source import UPSTREAM_REVISION, verify_source

from .skillclaw_config import load_config
from .skillclaw_process import NativeProcess
from .skillclaw_sessions import NativeCodexLaunch, copy_bundles, inventory


def check_aer_config(config, baseline):
    if (config.agent.plugin != "codex_cli_agent" or config.agent.config.get("skills") is not None
        or config.agent.config.get("profile") or not config.agent.config.get("ignore_user_config", True)):
        raise ValueError("Native baseline requires Codex, skills: null, ignore_user_config: true and no profile")
    if config.evolution.mode != "disabled" or config.targets.allowed or config.routing.plugin is not None:
        raise ValueError("Native baseline requires disabled AER evolution and routing")
    if baseline.mode == "learn" and config.benchmark.execution_split != "dev":
        raise ValueError("Native learning only accepts dev tasks; use frozen mode for test evaluation")
    if baseline.mode == "learn" and config.agent.initial_snapshot_ref is not None:
        raise ValueError("Native learning requires a fresh AER execution snapshot")


def run(config_path, *, prepare_only=False):
    baseline = load_config(config_path)
    verify_source(baseline.source)
    loaded = load_experiment_config(baseline.aer_config)
    check_aer_config(loaded.config, baseline)
    initial = inventory(baseline.initial_library)
    if prepare_only:
        # No model calls, native worker, task execution, or persistent run directory.
        return {"status": "configuration_checked", "baseline": "skillclaw_native_workflow",
            "upstream_revision": UPSTREAM_REVISION, "config": baseline.model_dump(), "initial_library": initial}
    prepared = prepare_execution(baseline.aer_config)
    if not isinstance(prepared.agent, CodexCliAgent):
        raise ValueError("Native SkillClaw baseline currently supports Codex task execution")
    config = prepared.benchmark.loaded_config.config
    root = prepared.storage.run_root / "skillclaw-baseline"
    root.mkdir()
    initial = copy_bundles(baseline.initial_library, root / "library")
    copy_bundles(root / "library", root / "libraries" / initial["digest"])
    tasks = prepared.benchmark.execution_tasks
    stable = prepared.storage.version_store.stable()
    execution_model = prepared.agent.model_adapter.read(stable, prepared.storage.artifact_store).model_id
    if config.benchmark.task_ids:
        by_id = {task.task_id: task for task in tasks}
        tasks = tuple(by_id[task_id] for task_id in config.benchmark.task_ids)
    manifest = {"baseline": "skillclaw_native_workflow", "status": "running",
        "upstream_revision": UPSTREAM_REVISION, "source_patches": [],
        "adapter": "responses_metadata_v1", "mode": baseline.mode,
        "schedule": {"evolve_every_tasks": baseline.evolve_every_tasks, "flush_tail": True},
        "config": baseline.model_dump(), "execution_model": execution_model,
        "aer_snapshot_ref": str(prepared.storage.run_root / prepared.storage.initial_snapshot_ref.uri),
        "dataset_version": prepared.benchmark.benchmark.dataset_version,
        "scorer_version": prepared.benchmark.evaluator.scorer_version,
        "task_order": [task.task_id for task in tasks], "initial_library": initial,
        "tasks": [], "cycles": [], "evolution_usage": None,
        "total_token_budget_enforced": False,
        "native_options": {"engine": "workflow", "retrieval_mode": "template", "publish_mode": "direct",
                           "storage_backend": "local", "use_prm": False, "session_idle_close_seconds": 0},
        "aer_scores_sent_to_skillclaw": False, "aer_candidate_promotion": False}
    path = root / "baseline.json"
    def save():
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
    def checkpoint():
        current = inventory(root / "library")
        destination = root / "libraries" / current["digest"]
        if not destination.exists():
            copy_bundles(root / "library", destination)
        return current
    save()
    print("[SkillClaw baseline] " + str(path), flush=True)
    try:
        with NativeProcess(baseline, root, execution_model) as native:
            names = [value for endpoint in (baseline.execution_api, baseline.evolution_api)
                     for value in endpoint.model_dump().values() if value]
            adapter = NativeCodexLaunch(native, root / "library", credential_names=names)
            prepared.agent.launch_adapter = adapter
            executor = ScoredTaskExecutor(prepared.agent, prepared.benchmark.benchmark, prepared.benchmark.evaluator)
            for index, task in enumerate(tasks, 1):
                current = checkpoint()
                context = RunContext(run_id=prepared.storage.run_id, episode_id=timestamp_id("baseline"),
                    operation_id=timestamp_id("execution"), stage="initial_execution", seed=config.experiment.seed,
                    budget=None, workspace_ref=str(prepared.storage.run_root),
                    model_timeout_seconds=config.limits.model_call_timeout_seconds,
                    tool_timeout_seconds=config.limits.tool_call_timeout_seconds,
                    max_output_tokens=config.limits.max_output_tokens_per_call)
                identity = sha256((stable.version_id + ":" + current["digest"]).encode()).hexdigest()
                snapshot = stable.replace(version_id="skillclaw-" + identity)
                print(f"[任务 {index}/{len(tasks)}] {task.task_id} library={current['digest'][:12]}", flush=True)
                before = len(adapter.sessions)
                scored = executor.execute(task, snapshot, context)
                record = root / f"task-{index:04d}.json"
                record.write_text(scored.model_dump_json(indent=2), encoding="utf-8")
                session = adapter.sessions[-1] if len(adapter.sessions) > before else None
                manifest["tasks"].append({"task_id": task.task_id, "record": str(record),
                    "library_digest": current["digest"], "session_id": session.session_id if session else None,
                    "score": scored.evaluation.score if scored.evaluation else None,
                    "execution_status": scored.execution.status if scored.execution else "error"})
                save()
                if session is None or session.failure or not session.finished:
                    raise RuntimeError("Task did not complete native session collection; inspect task record/native.log")
                if scored.error is not None:
                    # Benchmark failures have a valid evaluation and are learning evidence.
                    # Execution/integration failures are not silently treated as scored failures.
                    raise RuntimeError("Task execution/scoring error; native session retained for inspection")
                if baseline.mode == "learn" and (index % baseline.evolve_every_tasks == 0 or index == len(tasks)):
                    print("[SkillClaw] 消费原生会话队列并进化技能…", flush=True)
                    result = native.request("cycle")
                    updated = checkpoint()
                    manifest["cycles"].append({"after_task": index, "before_library": current["digest"],
                        "after_library": updated["digest"],
                        "removed_files": sorted(set(current["files"]) - set(updated["files"])),
                        "changed_files": sorted(name for name, digest in updated["files"].items()
                                                if current["files"].get(name) != digest), **result})
                    save()
                    if result["summary"].get("had_processing_error"):
                        raise RuntimeError("Native evolution reported processing errors; queue retained")
        manifest["status"] = "completed"
    except BaseException as exc:
        manifest.update(status="interrupted" if isinstance(exc, KeyboardInterrupt) else "error",
                        error_type=type(exc).__name__, error=str(exc))
        raise
    finally:
        manifest["final_library"] = checkpoint()
        save()
        prepared.agent.launch_adapter = None
    return {"status": manifest["status"], "report": str(path), "library": str(root / "library")}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = run(args.config, prepare_only=args.prepare_only)
    except (ValueError, RuntimeError, OSError) as exc:
        parser.exit(1, str(exc) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
