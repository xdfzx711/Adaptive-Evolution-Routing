"""Independent execution, evaluation and diagnosis, without an evolution loop."""

from dataclasses import dataclass, replace
from pathlib import Path
from aer_core.identifiers import timestamp_id
from aer_core.feedback import validate_diagnosis

from aer_core.contracts.agent import AgentPlugin
from aer_core.contracts.diagnoser import DiagnoserPlugin
from aer_core.errors import AERError, DiagnosisError, EvaluationError, ExecutionError
from aer_core.plugin_manager import BuiltPlugins, PluginManager
from aer_core.schemas.assessment import TaskExecutionRecord
from aer_core.schemas.runtime import RunContext
from aer_core.schemas.states import RunStage
from aer_core.schemas.usage import ResourceUsage
from aer_core.usage import sum_usage
from aer_core.task_executor import ScoredTaskExecutor
from plugins.storage.layout import RunLayout, compact_plugins, write_manifest

from .benchmark import PreparedBenchmark, prepare_benchmark
from .bootstrap import agent_bindings, component_binding, create_registry, evolution_bindings, with_providers
from .feedback import context_builder
from .storage import PreparedStorage, prepare_storage
from .environment import provider_dependencies, resolve_model


@dataclass(frozen=True)
class PreparedExecution:
    benchmark: PreparedBenchmark
    storage: PreparedStorage
    agent: AgentPlugin
    diagnoser: DiagnoserPlugin
    built_plugins: BuiltPlugins

    def run_task(self, task_id: str) -> TaskExecutionRecord:
        task = next((task for task in self.benchmark.execution_tasks if task.task_id == task_id), None)
        if task is None:
            raise ExecutionError("Task is outside configured execution selection", stage="setup")
        config = self.benchmark.loaded_config.config
        context = RunContext(run_id=self.storage.run_id, episode_id=timestamp_id("episode"),
            operation_id=timestamp_id("execution"), stage=RunStage.INITIAL_EXECUTION,
            seed=config.experiment.seed, budget=None, workspace_ref=str(self.storage.run_root),
            model_timeout_seconds=config.limits.model_call_timeout_seconds,
            tool_timeout_seconds=config.limits.tool_call_timeout_seconds,
            max_output_tokens=config.limits.max_output_tokens_per_call)
        RunLayout(self.storage.run_root).begin_episode(task.identity, context)
        task_view = self.benchmark.benchmark.task_view(task)
        scored = ScoredTaskExecutor(self.agent, self.benchmark.benchmark, self.benchmark.evaluator).execute(
            task, self.storage.version_store.stable(), context)
        if scored.execution is None:
            raise ExecutionError("Agent did not return an associated execution result", stage="initial_execution", usage=scored.usage)
        result, evaluation = scored.execution, scored.evaluation
        feedback, diagnosis = None, None
        assessment_usage = ResourceUsage.zero()
        error = scored.error if result.status in {"completed", "step_limit"} else None
        if evaluation is not None:
            assessment_context = replace(context, stage=RunStage.DIAGNOSIS, operation_id=context.operation_id + "/diagnosis")
            try:
                builder = context_builder(self, task)
                public_task, execution_view, feedback = builder.public_views(task_view, result, evaluation)
                if config.benchmark.execution_split == "dev":
                    diagnosis = validate_diagnosis(self.diagnoser.diagnose(public_task, execution_view, feedback, assessment_context), execution_view, feedback)
                    assessment_usage = diagnosis.usage or ResourceUsage.zero()
            except AERError as failure:
                error = failure.to_info()
                assessment_usage = error.usage or ResourceUsage.zero()
                diagnosis = None
            except Exception as failure:
                error_type = DiagnosisError if assessment_context.stage == RunStage.DIAGNOSIS else EvaluationError
                error = error_type(str(failure) or type(failure).__name__, stage=assessment_context.stage,
                                   operation_id=assessment_context.operation_id).to_info()
        return TaskExecutionRecord(run_id=context.run_id, episode_id=context.episode_id, task=task.identity,
            execution=result, evaluation=evaluation, feedback=feedback, diagnosis=diagnosis, assessment_error=error,
            assessment_usage=assessment_usage)

    def save_record(self, record: TaskExecutionRecord) -> dict:
        if record.run_id != self.storage.run_id:
            raise ExecutionError("Execution record belongs to another run", stage="setup")
        ref = self.storage.artifact_store.put(record.model_dump_json(indent=2).encode("utf-8"), "task_execution")
        return {"status": "executed", "run_root": str(self.storage.run_root),
            "dataset_version": self.benchmark.benchmark.dataset_version,
            "scorer_version": self.benchmark.evaluator.scorer_version,
            "record_ref": ref.model_dump(mode="json"), "record": record.model_dump(mode="json"),
            "usage": sum_usage([record.execution.usage, record.evaluation.usage if record.evaluation and record.evaluation.usage else ResourceUsage.zero(), record.assessment_usage]).model_dump(mode="json"),
            "notes": ["One fresh-session execution and external evaluation; development tasks also receive public-evidence diagnosis.",
                      "Final test feedback is not passed to the Diagnoser.",
                      "No evolution, candidate validation or stable version commit was performed.",
                      "Model usage sources distinguish provider-reported and simulated tokens; resource budgets are not enforced."]}


def prepare_execution(path: str | Path, *, run_id: str | None = None) -> PreparedExecution:
    benchmark = prepare_benchmark(path)
    benchmark = replace(benchmark, loaded_config=resolve_model(benchmark.loaded_config, required=True))
    config = benchmark.loaded_config.config
    registry = create_registry()
    bindings = agent_bindings(config, registry)
    bindings.append(component_binding("diagnoser", "diagnoser", config.assessment.diagnoser, {}, registry))
    # Providers may be required only by the Diagnoser, Router or Evolver.
    plan = with_providers(config, registry, [*bindings, *evolution_bindings(config, registry)])
    bindings = [binding for binding in plan if binding.category in {"agent", "diagnoser", "model_provider", "tool_provider"}]
    # Resolve factories before creating a persistent run directory.
    for binding in bindings:
        registry.get(binding.category, binding.plugin_id)
    shared = {name: benchmark.built_plugins.instances[name] for name in ("model", "tools")
              if name in benchmark.built_plugins.instances}
    bindings = [binding for binding in bindings if binding.name not in shared]
    dependencies = provider_dependencies(benchmark.loaded_config, bindings, supplied=shared)
    storage = prepare_storage(path, run_id=run_id, loaded_config=benchmark.loaded_config)
    dependencies.update(initial_snapshot=storage.version_store.stable(), artifact_store=storage.artifact_store)
    built = PluginManager(registry).build(bindings, dependencies=dependencies)
    if shared:
        shared_records = [record for record in benchmark.built_plugins.manifest.instances if record.binding_name in shared]
        built = BuiltPlugins({**shared, **built.instances}, built.manifest.replace(instances=[*shared_records, *built.manifest.instances]))
    model = built.instances.get("model")
    runtime_info = model.runtime_info() if callable(getattr(model, "runtime_info", None)) else None
    benchmark_info = benchmark.as_dict()
    for key in ("assembly_manifest", "notes", "source_path", "status", "index"):
        benchmark_info.pop(key, None)
    write_manifest(storage.run_root, "execution", {
        "schema_version": "aer.execution.v0.1", "scope": "single_task_execution",
        "dataset_version": benchmark.benchmark.dataset_version, "scorer_version": benchmark.evaluator.scorer_version,
        "benchmark": benchmark_info, "plugins": compact_plugins(built.manifest),
        "budget_enforced": False,
        "model_runtime": runtime_info,
    })
    return PreparedExecution(benchmark, storage, built.instances["agent"], built.instances["diagnoser"], built)
