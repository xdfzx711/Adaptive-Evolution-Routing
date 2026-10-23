"""Execution workspace and collected output, independent of any Agent SDK."""

from pathlib import Path
from dataclasses import dataclass

from pydantic import Field

from .base import DomainModel, Items, JsonObject
from .references import ArtifactRef


@dataclass(frozen=True)
class WorkspaceHandle:
    path: Path
    baseline_revision: str | None = None
    source_revision: str | None = None


class CollectedResult(DomainModel):
    output: JsonObject
    artifact_refs: Items[ArtifactRef] = ()
    state: JsonObject = Field(default_factory=dict)
