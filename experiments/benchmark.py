"""Load actual benchmark partitions independently of Agent execution."""

from dataclasses import dataclass
import json
from pathlib import Path

from aer_core.config.loader import LoadedConfig
from aer_core.config.partitions import validate_partitions
from aer_core.errors import ConfigError, ContractError, EvaluationError
from aer_core.contracts.benchmark import BenchmarkPlugin, TaskValidationProvider
from aer_core.contracts.evaluator import EvaluatorPlugin
from aer_core.feedback import PublicFeedbackBuilder
from aer_core.schemas.feedback import ContextPolicy, TaskFeedbackPolicy
from aer_core.plugin_manager import BuiltPlugins, PluginManager
from aer_core.schemas.execution import EvaluationResult, ExecutionResult
from aer_core.schemas.task import EvaluationSpec, Task
from aer_core.schemas.runtime import RunContext
from aer_core.schemas.validation import evaluations_match

from .bootstrap import component_binding, create_registry, with_providers
from .configuration import load_experiment_config
from .environment import provider_dependencies, resolve_model


@dataclass(frozen=True)
class PreparedBenchmark:
    loaded_config: LoadedConfig
    benchmark: BenchmarkPlugin
    evaluator: EvaluatorPlugin
    execution_tasks: tuple[Task, ...]
    similar_tasks: tuple[Task, ...]
    regression_tasks: tuple[Task, ...]
    built_plugins: BuiltPlugins

    def validation_groups(self, task: Task) -> tuple[tuple[Task, ...], tuple[Task, ...]]:
        """Resolve an optional plugin selection without coupling the runner to a dataset."""
        if task not in self.execution_tasks:
            raise ContractError("Validation task is outside the configured execution selection", stage="setup")
        if not isinstance(self.benchmark, TaskValidationProvider):
            return self.similar_tasks, self.regression_tasks
        selected = self.benchmark.validation_tasks(task)
        available = (*self.similar_tasks, *self.regression_tasks)
        if (not isinstance(selected, tuple) or any(not isinstance(item, Task) or item not in available for item in selected)
                or len({item.identity for item in selected}) != len(selected)):
            raise ContractError("Task-specific validation must select unique tasks from the validation partition", stage="setup")
        similar = tuple(item for item in selected if item in self.similar_tasks)
        regression = tuple(item for item in selected if item in self.regression_tasks)
        if not similar or not regression:
            raise ContractError(f"Task {task.task_id} requires both similar and regression validation tasks", stage="setup")
        return similar, regression

    def as_dict(self) -> dict:
        config = self.loaded_config.config
        index = self.benchmark.index()
        return {
            "status": "benchmark_ready", "source_path": str(self.loaded_config.source_path),
            "benchmark_id": index.benchmark_id, "dataset_version": self.benchmark.dataset_version,
            "scorer_version": self.evaluator.scorer_version,
            "score_policy": self.evaluator.score_policy,
            "execution_split": config.benchmark.execution_split,
            "execution_task_ids": [task.task_id for task in self.execution_tasks],
            "validation_task_ids": {
                config.validation.similar_group: [task.task_id for task in self.similar_tasks],
                config.validation.regression_group: [task.task_id for task in self.regression_tasks],
            },
            "validation_tasks_by_execution_task": {
                task.task_id: {config.validation.similar_group: [item.task_id for item in groups[0]],
                               config.validation.regression_group: [item.task_id for item in groups[1]]}
                for task in self.execution_tasks
                for groups in [self.validation_groups(task)]
            } if config.evolution.mode != "disabled" else {},
            "index": index.model_dump(mode="json"),
            "public_tasks": [self.benchmark.task_view(task).model_dump(mode="json") for task in self.execution_tasks],
            "assembly_manifest": self.built_plugins.manifest.model_dump(mode="json"),
            "notes": ["Actual packaged task partitions and scoring specifications were checked.",
                      "No Agent execution, evolution or paired validation was performed.",
                      "Configured resource budgets are not enforced in this MVP stage."],
        }

    def score(self, result: ExecutionResult, *, operation_id: str) -> EvaluationResult:
        task = next((task for task in self.execution_tasks if task.task_id == result.task_id), None)
        if task is None:
            raise EvaluationError("Execution task is outside the configured task selection", stage="evaluation",
                                  operation_id=operation_id, plugin_id=self.loaded_config.config.assessment.evaluator)
        config = self.loaded_config.config
        context = RunContext(run_id=config.experiment.id, episode_id=result.operation_id,
            operation_id=operation_id, stage="evaluation", seed=config.experiment.seed, budget=None,
            workspace_ref=config.storage.root, model_timeout_seconds=config.limits.model_call_timeout_seconds,
            tool_timeout_seconds=config.limits.tool_call_timeout_seconds, max_output_tokens=config.limits.max_output_tokens_per_call)
        spec = self.benchmark.evaluation_spec(task)
        if not isinstance(spec, EvaluationSpec) or spec.task != task.identity:
            raise ContractError("Scoring specification must match the selected task", stage="evaluation")
        evaluation = self.evaluator.evaluate(task, result, spec, context)
        if not isinstance(evaluation, EvaluationResult) or not evaluations_match(result, evaluation) or evaluation.operation_id != operation_id:
            raise ContractError("Evaluator returned mismatched execution identities", stage="evaluation")
        return evaluation

    def public_feedback(self, evaluation):
        from aer_core.scoring_feedback import scorer_feedback_policy
        task = next((task for task in self.execution_tasks if task.task_id == evaluation.task_id), None)
        if task is None:
            raise EvaluationError("Evaluation task is outside the configured selection", stage="evaluation")
        declaration = self.benchmark.feedback_policy(task)
        if not isinstance(declaration, TaskFeedbackPolicy) or declaration.task != task.identity:
            raise ContractError("Feedback declaration must match the task", stage="evaluation")
        return PublicFeedbackBuilder(ContextPolicy(output_fields=declaration.output_fields,
            **scorer_feedback_policy(self.evaluator, self.loaded_config.config.assessment.evaluator,
                                     assessment_version=evaluation.assessment_version),
            reason_messages={**declaration.reason_messages, **(getattr(self.evaluator, "public_reason_messages", {})
                if evaluation.assessment_version in {None, self.evaluator.scorer_version} else {})})).feedback(evaluation)


def load_execution(path: Path) -> ExecutionResult:
    def unique_keys(pairs):
        data = {}
        for key, value in pairs:
            if key in data:
                raise ValueError(f"duplicate JSON key in execution: {key}")
            data[key] = value
        return data

    def invalid_constant(value):
        raise ValueError(f"nonfinite JSON number in execution: {value}")

    data = json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=unique_keys,
                      parse_constant=invalid_constant)
    return ExecutionResult.model_validate(data)


def prepare_benchmark(path: str | Path) -> PreparedBenchmark:
    loaded = load_experiment_config(path)
    config = loaded.config
    registry = create_registry()
    bindings = with_providers(config, registry, [
        component_binding("benchmark", "benchmark", config.benchmark.plugin, config.benchmark.config, registry),
        component_binding("evaluator", "evaluator", config.assessment.evaluator, config.assessment.evaluator_config, registry)])
    loaded = resolve_model(loaded, required=any(binding.name == "model" for binding in bindings))
    built = PluginManager(registry).build(bindings, dependencies=provider_dependencies(loaded, bindings))
    benchmark = built.instances["benchmark"]
    evaluator = built.instances["evaluator"]
    from .native_scoring import validate_native_scoring
    validate_native_scoring(benchmark, config, registry)
    benchmark_id = registry.get("benchmark", config.benchmark.plugin).metadata.capabilities.benchmark_id
    issues = validate_partitions(config, benchmark.index(), benchmark_id)
    if issues:
        raise ConfigError(loaded.source_path, issues)
    tasks = benchmark.tasks(config.benchmark.execution_split)
    if config.benchmark.task_ids is not None:
        selected = set(config.benchmark.task_ids)
        tasks = tuple(task for task in tasks if task.task_id in selected)
    validation = config.validation
    independent = benchmark.tasks(validation.split)
    similar = tuple(task for task in independent if task.metadata.get(validation.group_field) == validation.similar_group)
    regression = tuple(task for task in independent if task.metadata.get(validation.group_field) == validation.regression_group)
    prepared = PreparedBenchmark(loaded, benchmark, evaluator, tasks, similar, regression, built)
    if config.evolution.mode != "disabled":
        for task in tasks:
            prepared.validation_groups(task)
    return prepared
