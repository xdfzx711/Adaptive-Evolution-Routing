from typing import Protocol, runtime_checkable

from aer_core.schemas.evolution import AgentCapabilities
from aer_core.schemas.execution import ExecutionResult
from aer_core.schemas.references import AgentSnapshot
from aer_core.schemas.runtime import RunContext
from aer_core.schemas.task import TaskView


@runtime_checkable
class AgentPlugin(Protocol):
    """Run a fresh session from Snapshot; do not mutate it or grade correctness.

    snapshot/restore cover declared controlled state, never a live budget or
    hidden model reasoning. Raise UnsupportedCapability for unsupported restore
    and ExecutionError for failures without a usable execution result.
    """

    def capabilities(self) -> AgentCapabilities: ...

    def run(self, task: TaskView, snapshot: AgentSnapshot, run_ctx: RunContext) -> ExecutionResult: ...

    def snapshot(self) -> AgentSnapshot: ...

    def restore(self, snapshot: AgentSnapshot) -> None: ...
