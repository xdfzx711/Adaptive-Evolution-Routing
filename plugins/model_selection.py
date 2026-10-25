"""Agent-independent execution-model selections and their plugin adapter contract."""

from typing import Protocol, runtime_checkable

from aer_core.contracts.storage import ArtifactStore
from aer_core.schemas.base import DomainModel, Identifier
from aer_core.schemas.references import AgentSnapshot


class ModelSelection(DomainModel):
    model_id: Identifier
    # None deliberately preserves the backend's default, including legacy artifacts.
    reasoning_effort: Identifier | None = None


@runtime_checkable
class ExecutionModelAdapter(Protocol):
    @property
    def compatible_agents(self) -> tuple[str, ...]: ...

    def read(self, snapshot: AgentSnapshot, artifacts: ArtifactStore) -> ModelSelection: ...

    def options(self, snapshot: AgentSnapshot) -> tuple[ModelSelection, ...]: ...

    def encode(self, selection: ModelSelection) -> bytes: ...
