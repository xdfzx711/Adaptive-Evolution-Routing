"""Startup, per-episode version scopes and CLI records for the controller."""

from dataclasses import dataclass, replace
from pathlib import Path

from aer_core.errors import ExecutionError
from aer_core.identifiers import timestamp_id
from aer_core.orchestrator import Orchestrator
from aer_core.plugin_manager import PluginManager
from aer_core.schemas.experiment import RunResult
from aer_core.schemas.plugins import PluginBinding
from aer_core.schemas.runtime import RunContext
from aer_core.schemas.task import Task
from aer_core.task_executor import ScoredTaskExecutor
from aer_core.cached_executor import CachedTaskExecutor
from plugins.validation.cache import LocalValidationCache
from plugins.storage._files import FileRoot, canonical_bytes
from plugins.storage.layout import RunLayout, write_manifest
from .bootstrap import create_registry
from .evolution import PreparedEvolution, assemble_evolution
from .execution import PreparedExecution, prepare_execution
from .validation import PreparedValidation, assemble_validation, validation_conditions
from .reporting import RunReporter
from .feedback import context_builder


@dataclass(frozen=True)
class PreparedTask:
    task: Task
    context: RunContext
    scope_id: str
    execution: PreparedExecution
    evolution: PreparedEvolution | None
    validation: PreparedValidation | None
    controller: Orchestrator

    def run(self) -> RunResult:
        return self.controller.run_task(self.task, self.execution.benchmark.benchmark.task_view(self.task), self.context,
            allowed_history_task_ids=[item.task_id for item in self.execution.benchmark.benchmark.tasks("dev")])


@dataclass(frozen=True)
class PreparedOrchestrator:
    execution: PreparedExecution
    console: bool = False

    def prepare_task(self, task_id: str) -> PreparedTask:
        base = self.execution
        task = next((item for item in base.benchmark.execution_tasks if item.task_id == task_id), None)
        if task is None:
            raise ExecutionError("Task is outside configured task selection", stage="setup")
        config, storage = base.benchmark.loaded_config.config, base.storage
        episode_id = timestamp_id("episode")
        layout = RunLayout(storage.run_root)
        layout.select(task.task_id, episode_id)
        reporter = RunReporter(storage.run_root, console=self.console)
        scope_id = "orchestrator/continual" if config.state.task_mode == "continual" else "orchestrator/" + episode_id
        built = PluginManager(create_registry()).build([
            PluginBinding(name="versions", category="version_store", plugin_id=config.storage.versions,
                config={"root": str(storage.run_root), "scope_id": scope_id},
                dependency_bindings={"artifact_store": "artifacts", "experience_store": "experience"})],
            dependencies={"artifacts": storage.artifact_store, "experience": storage.experience_store})
        versions = built.instances["versions"]
        initial = storage.version_store.load(storage.initial_snapshot_ref.record_id)
        versions.initialize(initial)
        scoped = replace(base, storage=replace(storage, version_store=versions))
        evolution = assemble_evolution(scoped, progress=reporter) if config.evolution.mode != "disabled" else None
        conditions = validation_conditions(scoped)
        validation = assemble_validation(scoped, progress=reporter, conditions=conditions) if config.evolution.mode != "disabled" else None
        context = RunContext(run_id=storage.run_id, episode_id=episode_id, operation_id=episode_id,
            stage="initial_execution", seed=config.experiment.seed, budget=None,
            workspace_ref=str(storage.run_root / "workspaces" / episode_id),
            model_timeout_seconds=config.limits.model_call_timeout_seconds,
            tool_timeout_seconds=config.limits.tool_call_timeout_seconds,
            max_output_tokens=config.limits.max_output_tokens_per_call)
        reporter.configure(scoped.benchmark.benchmark.task_view(task), config, context, storage.artifact_store, versions)
        controller = Orchestrator(config=config,
            executor=CachedTaskExecutor(ScoredTaskExecutor(scoped.agent, scoped.benchmark.benchmark, scoped.benchmark.evaluator),
                                        LocalValidationCache(storage.run_root), conditions),
            diagnoser=scoped.diagnoser, contexts=context_builder(scoped, task,
                history_max_attempts=config.evolution.history_max_attempts,
                history_max_text_chars=config.evolution.history_max_text_chars,
                history_max_change_chars=config.evolution.history_max_change_chars,
                history_max_construction_chars=config.evolution.history_max_construction_chars),
            artifacts=storage.artifact_store, versions=versions, experience=storage.experience_store,
            evolution=evolution, validation=validation, progress=reporter, storage_placement=layout)
        FileRoot(storage.run_root).immutable(layout.episode_path(episode_id), canonical_bytes({
            "schema_version": "aer.episode.v0.1", "task": task.identity.model_dump(mode="json"),
            "scope_id": scope_id, "task_mode": config.state.task_mode,
            "initial_version_id": versions.stable().version_id, "run_context": context.to_record().model_dump(mode="json"),
            "version_store": {"plugin": config.storage.versions}}))
        return PreparedTask(task, context, scope_id, scoped, evolution, validation, controller)

    def run_task(self, task_id: str) -> RunResult:
        return self.prepare_task(task_id).run()

    def save_record(self, result: RunResult) -> dict:
        storage = self.execution.storage
        if result.run_id != storage.run_id:
            raise ExecutionError("RunResult belongs to another run", stage="setup")
        layout = RunLayout(storage.run_root)
        manifest = FileRoot(storage.run_root).read_json(layout.episode_path(result.episode_id))
        layout.select(result.task_id, result.episode_id, result.evolution_rounds_used or None)
        ref = storage.artifact_store.put(result.model_dump_json(indent=2).encode("utf-8"), "run_result")
        return {"status": "task_finished", "run_root": str(storage.run_root), "stable_scope_id": manifest["scope_id"],
            "log_path": str(storage.run_root / "evolution.txt"),
            "round_records_path": str(storage.run_root / "rounds") if (storage.run_root / "rounds").exists() else None,
            "record_ref": ref.model_dump(mode="json"), "record": result.model_dump(mode="json"),
            "notes": ["One complete task evolution episode, including commit and reuse of validated task results.",
                      "Resource hard limits and consecutive-no-commit stopping remain deferred."]}


def prepare_orchestrator(path: str | Path, *, run_id: str | None = None, console: bool = False) -> PreparedOrchestrator:
    execution = prepare_execution(path, run_id=run_id)
    write_manifest(execution.storage.run_root, "orchestrator", {
        "schema_version": "aer.orchestrator.v0.1", "plugin_api": execution.built_plugins.manifest.api_version,
        "budget_enforced": False, "incomplete_validation": "stop_without_commit",
        "evolution_rounds": "attempt_started", "post_commit_execution": "reuse_validated_current_task_result",
        "task_mode": execution.benchmark.loaded_config.config.state.task_mode})
    return PreparedOrchestrator(execution, console=console)
