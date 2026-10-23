"""Local SWE-bench rows, with explicit AER roles and private gold patches."""

from hashlib import sha256
import json
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from aer_core.config.models import NonBlank, Settings, UniqueStrings
from aer_core.config.partitions import BenchmarkIndex, IndexTask
from aer_core.errors import BenchmarkError, UnsupportedCapability
from aer_core.plugin_registry import API_VERSION
from aer_core.schemas.feedback import TaskFeedbackPolicy
from aer_core.schemas.plugins import PluginMetadata
from aer_core.schemas.task import Task, TaskView, EvaluationSpec
from plugins.evaluators.static_patch import StaticPatchEvaluator, StaticPatchSettings
from plugins.workspace.patches import patch_changes


class ValidationSelection(Settings):
    similar: UniqueStrings = Field(min_length=1)
    regression: UniqueStrings = Field(min_length=1)


class SwebenchSettings(Settings):
    dataset_path: NonBlank
    roles: dict[NonBlank, Literal["dev", "similar", "regression", "test"]]
    validation_sets: dict[NonBlank, ValidationSelection] = Field(default_factory=dict)

    @model_validator(mode="after")
    def check_validation_sets(self):
        if not self.validation_sets:
            return self
        development = {key for key, role in self.roles.items() if role == "dev"}
        if set(self.validation_sets) != development:
            raise ValueError("validation_sets must define exactly one selection for every dev task")
        for task_id, selection in self.validation_sets.items():
            for group in ("similar", "regression"):
                for validation_id in getattr(selection, group):
                    if self.roles.get(validation_id) != group:
                        raise ValueError(f"validation_sets.{task_id}.{group}: {validation_id} must have role {group}")
        return self


class SwebenchAdapter:
    benchmark_id = "swebench"

    def __init__(self, settings, config_root):
        self._validation_sets = settings.validation_sets
        path = Path(settings.dataset_path).expanduser()
        path = (config_root / path).resolve() if not path.is_absolute() else path.resolve()
        try:
            contents = path.read_bytes()
            document = json.loads(contents)
            records = document["instances"] if isinstance(document, dict) else document
            if not isinstance(records, list) or not settings.roles:
                raise ValueError("provide instances and an explicit nonempty roles mapping")
            by_id = {row["instance_id"]: row for row in records}
            if len(by_id) != len(records) or not set(settings.roles) <= set(by_id):
                raise ValueError("instances must have unique IDs and include every configured role")
            self._tasks, self._specs = {}, {}
            self._splits = {"dev": [], "validation": [], "test": []}
            for instance_id, role in settings.roles.items():
                row = by_id[instance_id]
                for field in ("repo", "base_commit", "problem_statement", "patch"):
                    if not isinstance(row.get(field), str) or not row[field].strip():
                        raise ValueError(f"{instance_id} requires nonempty {field}")
                changes, _ = patch_changes(row["patch"])
                if not changes:
                    raise ValueError("reference patch has no textual edits")
                split = "validation" if role in {"similar", "regression"} else role
                metadata = {"source_id": instance_id, "split": split, "category": "code_repair", "repo": row["repo"]}
                if split == "validation":
                    metadata["validation_group"] = role
                task = Task(task_id=instance_id, benchmark_id=self.benchmark_id,
                    input={"instruction": "Fix the issue by editing this repository. This experiment uses static patch comparison: "
                        "do not install dependencies or run tests. Preserve unrelated behavior. End with a short summary of your changes.",
                        "repo": row["repo"], "base_commit": row["base_commit"], "problem_statement": row["problem_statement"]},
                    metadata=metadata, eval_spec_ref="static.patch.v1:" + instance_id)
                self._tasks[instance_id] = task
                self._splits[split].append(task)
                self._specs[instance_id] = EvaluationSpec(task=task.identity,
                    scoring_method="static.patch.v1", private_parameters={"reference_patch": row["patch"]})
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise BenchmarkError(f"Cannot load SWE-bench instances: {error}", stage="setup", plugin_id="swebench_adapter") from error
        self._splits = {key: tuple(value) for key, value in self._splits.items()}
        self.dataset_version = "swebench-local." + sha256(contents).hexdigest()[:16]
        self.source_paths = (path,)

    def tasks(self, split):
        if split not in self._splits:
            raise UnsupportedCapability("Unknown SWE-bench split", stage="setup", plugin_id="swebench_adapter")
        return self._splits[split]

    def task_view(self, task):
        if self._tasks.get(task.task_id) != task:
            raise BenchmarkError("Task does not belong to this SWE-bench snapshot", stage="setup")
        data = task.model_dump(mode="json")
        return TaskView(task_id=task.task_id, benchmark_id=task.benchmark_id,
                        input=data["input"], public_metadata=data["metadata"])

    def evaluation_spec(self, task):
        self.task_view(task)
        return self._specs[task.task_id]

    def validation_tasks(self, task):
        self.task_view(task)
        selection = self._validation_sets.get(task.task_id)
        if selection is None:
            return self.tasks("validation")
        return tuple(self._tasks[key] for key in (*selection.similar, *selection.regression))

    def index(self):
        return BenchmarkIndex(benchmark_id=self.benchmark_id, splits={split: [IndexTask(task_id=task.task_id,
            source_id=task.task_id, metadata=dict(task.metadata)) for task in tasks]
            for split, tasks in self._splits.items()})

    def feedback_policy(self, task):
        self.task_view(task)
        return TaskFeedbackPolicy(task=task.identity, allow_evolution=task in self.tasks("dev"),
            output_fields=["patch", "changed_files", "patch_valid", "syntax_valid", "syntax_checked_files", "syntax_error_files"],
            metadata_fields=["source_id", "split", "category", "repo"], reason_messages={
                "correct": "Static comparison met the threshold. This does not establish test correctness.",
                "static_match": "Static comparison met the threshold; tests were not run.",
                "static_partial_match": "The patch only partially matches the reference edits; tests were not run.",
                "empty_patch": "No source changes were collected.",
                "invalid_patch": "The collected patch is not a supported textual Git diff.",
                "syntax_error": "A modified Python file failed static syntax parsing."})


def build_swebench(*, config, dependencies):
    return SwebenchAdapter(config, dependencies.get("config_root"))


def build_static(*, config, dependencies):
    return StaticPatchEvaluator(config)


def register_swebench(registry):
    registry.register(PluginMetadata(id="swebench_adapter", category="benchmark", api_version=API_VERSION,
        plugin_version="0.1.1", dependencies=["config_root"],
        capabilities={"benchmark_id": "swebench", "splits": ["dev", "validation", "test"]}),
        build_swebench, config_model=SwebenchSettings, dependency_types={"config_root": Path})
    registry.register(PluginMetadata(id="static_patch_evaluator", category="evaluator", api_version=API_VERSION,
        plugin_version="0.1.0"), build_static, config_model=StaticPatchSettings)
