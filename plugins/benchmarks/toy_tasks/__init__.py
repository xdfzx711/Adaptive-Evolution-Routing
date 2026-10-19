"""Stable, packaged tasks with independently loaded private scoring data."""

import json
from importlib.resources import files
from pathlib import Path

from aer_core.config.partitions import BenchmarkIndex, IndexTask
from aer_core.errors import BenchmarkError, UnsupportedCapability
from aer_core.schemas.task import EvaluationSpec, Task, TaskIdentity, TaskView
from aer_core.schemas.feedback import TaskFeedbackPolicy

from .schemas import PrivateDataset, PublicDataset, validate_scoring


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _read(path, model):
    data = json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=_unique_keys,
                      parse_constant=_invalid_constant)
    return model.model_validate(data)


def _invalid_constant(value):
    raise ValueError(f"nonfinite JSON number: {value}")


class ToyBenchmark:
    benchmark_id = "toy_tasks"
    resource_package = __package__

    def __init__(self, tasks_path: Path | None = None, specs_path: Path | None = None):
        package = files(self.resource_package)
        public_path = tasks_path if tasks_path is not None else package.joinpath("tasks.json")
        private_path = specs_path if specs_path is not None else package.joinpath("scoring_specs.json")
        try:
            public = _read(public_path, PublicDataset)
            private = _read(private_path, PrivateDataset)
            if public.benchmark_id != self.benchmark_id or private.benchmark_id != self.benchmark_id:
                raise ValueError("public and private benchmark identities must match the selected plugin")
            if public.dataset_version != private.dataset_version:
                raise ValueError("public and private dataset versions differ")
            ids = [item.task_id for item in public.tasks]
            sources = [item.source_id for item in public.tasks]
            spec_ids = [item.task_id for item in private.specs]
            if len(ids) != len(set(ids)) or len(sources) != len(set(sources)):
                raise ValueError("task_id and source_id must be unique across all splits")
            if len(spec_ids) != len(set(spec_ids)) or set(ids) != set(spec_ids):
                raise ValueError("scoring specs must match tasks one-to-one")
            if {item.split for item in public.tasks} != {"dev", "validation", "test"}:
                raise ValueError("dev, validation and test must all be nonempty")
            if {item.validation_group for item in public.tasks if item.split == "validation"} != {"similar", "regression"}:
                raise ValueError("both validation groups must be nonempty")
            entries = {item.task_id: item for item in private.specs}
            tasks = {}
            specs = {}
            for item in public.tasks:
                entry = entries[item.task_id]
                validate_scoring(item.input, entry.scoring_method, entry.private_parameters)
                metadata = {"source_id": item.source_id, "split": item.split, "category": item.category}
                if item.family is not None:
                    metadata["family"] = item.family
                if item.validation_group is not None:
                    metadata["validation_group"] = item.validation_group
                tasks[item.task_id] = Task(task_id=item.task_id, benchmark_id=public.benchmark_id,
                    input=item.input.model_dump(mode="json", exclude_none=True), metadata=metadata,
                    eval_spec_ref=f"{private.scorer_version}:{item.task_id}")
                specs[item.task_id] = EvaluationSpec(task=TaskIdentity(benchmark_id=public.benchmark_id, task_id=item.task_id),
                    scoring_method=entry.scoring_method, private_parameters=entry.model_dump(mode="json")["private_parameters"])
        except (OSError, UnicodeError, ValueError) as error:
            raise BenchmarkError(f"Cannot load {self.benchmark_id}: {error}", stage="setup", plugin_id=self.benchmark_id) from error
        self.dataset_version = public.dataset_version
        self.scorer_version = private.scorer_version
        self._tasks = tasks
        self._specs = specs
        self._splits = {split: tuple(tasks[item.task_id] for item in public.tasks if item.split == split)
                        for split in ("dev", "validation", "test")}
        self.source_paths = tuple(Path(path).resolve() for path in (public_path, private_path) if isinstance(path, Path))

    def tasks(self, split: str) -> tuple[Task, ...]:
        if split not in self._splits:
            raise UnsupportedCapability(f"Unknown {self.benchmark_id} split: {split}", stage="setup", plugin_id=self.benchmark_id)
        return self._splits[split]

    def evaluation_spec(self, task: Task) -> EvaluationSpec:
        if self._tasks.get(task.task_id) != task:
            raise BenchmarkError("Task does not match the loaded benchmark", stage="evaluation", plugin_id=self.benchmark_id)
        return self._specs[task.task_id]

    def index(self) -> BenchmarkIndex:
        return BenchmarkIndex(benchmark_id=self.benchmark_id, splits={split: [IndexTask(task_id=task.task_id,
            source_id=task.metadata["source_id"], metadata=dict(task.metadata)) for task in tasks]
            for split, tasks in self._splits.items()})

    def task_view(self, task: Task) -> TaskView:
        if self._tasks.get(task.task_id) != task:
            raise BenchmarkError("Task does not match the loaded benchmark", stage="setup", plugin_id=self.benchmark_id)
        data = task.model_dump(mode="json")
        return TaskView(task_id=task.task_id, benchmark_id=task.benchmark_id, input=data["input"],
                        public_metadata=data["metadata"])

    def feedback_policy(self, task: Task) -> TaskFeedbackPolicy:
        self.task_view(task)  # Validate membership before declaring disclosure.
        from .schemas import ToyInput
        specification = ToyInput.model_validate(task.model_dump(mode="json")["input"])
        return TaskFeedbackPolicy(task=task.identity,
            output_fields=[field.name for field in specification.output_format.fields],
            metadata_fields=["category", "source_id", "split"],
            allow_evolution=task in self.tasks("dev"))
