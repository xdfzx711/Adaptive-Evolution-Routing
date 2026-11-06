from typing import Protocol, runtime_checkable
from pathlib import Path

from aer_core.config.partitions import BenchmarkIndex
from aer_core.schemas.feedback import TaskFeedbackPolicy
from aer_core.schemas.task import EvaluationSpec, Task, TaskView


@runtime_checkable
class BenchmarkPlugin(Protocol):
    """Return tasks in stable order and keep private scoring data separate.

    Unknown splits raise UnsupportedCapability. Task/validation partition
    checks are required at setup before a real run, not inferred from names.
    """

    def tasks(self, split: str) -> tuple[Task, ...]: ...

    def evaluation_spec(self, task: Task) -> EvaluationSpec: ...

    def task_view(self, task: Task) -> TaskView: ...

    dataset_version: str
    source_paths: tuple[Path, ...]

    def index(self) -> BenchmarkIndex: ...

    def feedback_policy(self, task: Task) -> TaskFeedbackPolicy: ...


@runtime_checkable
class TaskValidationProvider(Protocol):
    """Optional task-specific subset of the benchmark's validation partition.

    Tasks retain the configured group metadata. Benchmarks without this
    capability continue to use their complete shared validation partition.
    """

    def validation_tasks(self, task: Task) -> tuple[Task, ...]: ...


@runtime_checkable
class NativeEvaluationProvider(Protocol):
    """Optional native-scoring requirement; legacy benchmarks remain unchanged."""

    def native_evaluator_id(self) -> str: ...


@runtime_checkable
class PredictionExportProvider(Protocol):
    """Optional dataset-native prediction format, without accessing gold labels."""

    def export_predictions(self, outputs: dict[str, dict]) -> dict | list[dict]: ...
