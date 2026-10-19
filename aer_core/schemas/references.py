"""Record and artifact identities; these schemas perform no filesystem I/O."""

from pydantic import model_validator

from .base import DomainModel, Identifier, JsonObject, ReadOnlyMap


class RecordRef(DomainModel):
    record_id: Identifier
    uri: Identifier


class ManifestRef(RecordRef):
    pass


class ArtifactRef(DomainModel):
    artifact_id: Identifier
    kind: Identifier
    version: Identifier
    uri: Identifier
    digest: Identifier


class AgentSnapshot(DomainModel):
    version_id: Identifier
    agent_plugin_id: Identifier
    config: JsonObject
    artifacts: ReadOnlyMap[ArtifactRef]
    parent_version_id: Identifier | None = None

    @model_validator(mode="after")
    def parent_is_distinct(self):
        if self.parent_version_id == self.version_id:
            raise ValueError("a Snapshot cannot be its own parent")
        return self
