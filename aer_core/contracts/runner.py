from typing import Literal, Protocol, runtime_checkable

from aer_core.config.models import ExperimentConfig
from aer_core.schemas.experiment import ExperimentResult
from aer_core.schemas.task import Task


ResetScope = Literal["task", "episode", "benchmark"]


@runtime_checkable
class ExperimentRunner(Protocol):
    """Synchronous lifecycle; setup fixes plugins/config and validates task data.

    reset respects task_mode and never removes persisted evidence. close is
    safe to repeat. Runtime failures use AERError with stage/usage information.
    """

    def setup(self, config: ExperimentConfig) -> None: ...

    def run_task(self, task: Task) -> ExperimentResult: ...

    def reset(self, scope: ResetScope) -> None: ...

    def close(self) -> None: ...
