"""Agent-independent tool selections and the execution adapter contract."""

from typing import Protocol

from pydantic import field_validator

from aer_core.contracts.storage import ArtifactStore
from aer_core.schemas.base import DomainModel, Identifier, Items, JsonObject
from aer_core.schemas.references import AgentSnapshot


class ToolSelection(DomainModel):
    enabled_tools: Items[Identifier] = ()

    @field_validator("enabled_tools", mode="before")
    @classmethod
    def canonical_tools(cls, value):
        if not isinstance(value, (list, tuple)) or any(not isinstance(item, str) for item in value):
            raise ValueError("enabled_tools must be a list of tool identifiers")
        if len(set(value)) != len(value):
            raise ValueError("enabled_tools must be unique")
        return sorted(value)


class ToolDescriptor(DomainModel):
    description: str
    version: Identifier
    input_schema: JsonObject | None = None


class ExecutionToolAdapter(Protocol):
    @property
    def compatible_agents(self) -> tuple[str, ...]: ...

    def catalog(self, snapshot: AgentSnapshot) -> dict[str, ToolDescriptor]: ...

    def read(self, snapshot: AgentSnapshot, artifacts: ArtifactStore) -> ToolSelection: ...

    def encode(self, selection: ToolSelection) -> bytes: ...
