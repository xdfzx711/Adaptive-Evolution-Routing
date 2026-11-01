from pathlib import Path
from contextlib import AbstractContextManager
from typing import Protocol, runtime_checkable

from aer_core.schemas.runtime import RunContext
from aer_core.schemas.task import TaskView
from aer_core.schemas.workspace import CollectedResult, WorkspaceHandle
from aer_core.schemas.references import AgentSnapshot


@runtime_checkable
class WorkspaceProvider(Protocol):
    def prepare(self, task: TaskView, directory: Path, run_ctx: RunContext) -> WorkspaceHandle: ...

    def release(self, workspace: WorkspaceHandle) -> None: ...


@runtime_checkable
class ResultCollector(Protocol):
    def schema(self, task: TaskView) -> dict | None: ...

    def collect(self, task: TaskView, workspace: WorkspaceHandle, final_text: str,
                run_ctx: RunContext) -> CollectedResult: ...


@runtime_checkable
class EvolutionWorkspaceService(Protocol):
    """Development-only construction workspace; no version promotion capability."""

    def open(self, task: TaskView, snapshot: AgentSnapshot, run_ctx: RunContext) -> AbstractContextManager: ...
