"""Persistence envelopes used by local stores and startup initialization."""

from typing import Literal

from pydantic import model_validator

from .base import DomainModel, Identifier, JsonObject
from .experience import VersionCommitRecord
from .plugins import AssemblyManifest
from .references import AgentSnapshot, RecordRef
from .validation import ValidationReport


class SnapshotManifest(DomainModel):
    schema_version: Literal["aer.snapshot.v0.1"] = "aer.snapshot.v0.1"
    snapshot: AgentSnapshot
    snapshot_digest: Identifier


class CandidateManifest(DomainModel):
    schema_version: Literal["aer.candidate.v0.1"] = "aer.candidate.v0.1"
    candidate_id: Identifier
    parent_version_id: Identifier
    candidate_version_id: Identifier


class ValidationManifest(DomainModel):
    schema_version: Literal["aer.validation.v0.1"] = "aer.validation.v0.1"
    report: ValidationReport


class CommitJournal(DomainModel):
    schema_version: Literal["aer.commit.v0.1"] = "aer.commit.v0.1"
    request_digest: Identifier
    record: VersionCommitRecord
    validation_ref: RecordRef


class StablePointer(DomainModel):
    schema_version: Literal["aer.stable.v0.1"] = "aer.stable.v0.1"
    initial_version_id: Identifier
    version_id: Identifier
    last_commit: CommitJournal | None = None

    @model_validator(mode="after")
    def pointer_matches_commit(self):
        if self.last_commit is not None and self.last_commit.record.committed_version_id != self.version_id:
            raise ValueError("stable pointer must match its durable commit record")
        return self


class StorageManifest(DomainModel):
    schema_version: Literal["aer.storage.v0.1"] = "aer.storage.v0.1"
    run_id: Identifier
    experiment_id: Identifier
    source_config: Identifier
    experiment_config: JsonObject
    plugins: AssemblyManifest
    initial_snapshot_ref: RecordRef
