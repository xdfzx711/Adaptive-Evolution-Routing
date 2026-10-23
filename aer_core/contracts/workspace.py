from pathlib import Path
from typing import Protocol, runtime_checkable

from aer_core.schemas.runtime import RunContext
from aer_core.schemas.task import TaskView
from aer_core.schemas.workspace import CollectedResult, WorkspaceHandle


@runtime_checkable
class WorkspaceProvider(Protocol):
    def prepare(self, task: TaskView, directory: Path, run_ctx: RunContext) -> WorkspaceHandle: ...

    def release(self, workspace: WorkspaceHandle) -> None: ...


@runtime_checkable
class ResultCollector(Protocol):
    def schema(self, task: TaskView) -> dict | None: ...

    def collect(self, task: TaskView, workspace: WorkspaceHandle, final_text: str,
                run_ctx: RunContext) -> CollectedResult: ...
