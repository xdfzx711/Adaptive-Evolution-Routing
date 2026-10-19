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
