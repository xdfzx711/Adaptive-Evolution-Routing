from typing import Protocol, runtime_checkable

from aer_core.schemas.evolution import EvolutionCandidate
from aer_core.schemas.experience import CommitRequest, ExperienceEvent, ExperienceQuery, VersionCommitRecord
from aer_core.schemas.references import AgentSnapshot, ArtifactRef, RecordRef
from aer_core.schemas.validation import ValidationReport
from aer_core.schemas.task import TaskIdentity
from aer_core.schemas.runtime import RunContext


class StoragePlacement(Protocol):
    """Optional physical placement; independent of progress/report rendering."""

    def begin_episode(self, task: TaskIdentity, context: RunContext) -> None: ...

    def begin_round(self, number: int) -> None: ...


@runtime_checkable
class ArtifactStore(Protocol):
    """Store immutable bytes; retries may reuse content identity, never overwrite.

    Raise StorageError for unavailable/corrupt content and check referenced
    content identity when loading. The schema itself does not inspect a URI.
    """

    def put(self, content: bytes, kind: str) -> ArtifactRef: ...

    def get(self, ref: ArtifactRef) -> bytes: ...


@runtime_checkable
class VersionStore(Protocol):
    """Save immutable manifests, materialize isolated candidates and promote once.

    commit checks the expected stable parent and persists the exact validated
    version. A retry must not promote twice; CommitError leaves the old pointer
    intact unless durable recovery evidence establishes a successful commit.
    """

    def save(self, snapshot: AgentSnapshot) -> RecordRef: ...

    def load(self, version_id: str) -> AgentSnapshot: ...

    def initialize(self, snapshot: AgentSnapshot) -> RecordRef: ...

    def stable(self) -> AgentSnapshot: ...

    def materialize(self, stable: AgentSnapshot, candidate: EvolutionCandidate) -> AgentSnapshot: ...

    def save_validation(self, report: ValidationReport) -> RecordRef: ...

    def load_validation(self, ref: RecordRef) -> ValidationReport: ...

    def commit(self, request: CommitRequest) -> VersionCommitRecord: ...


@runtime_checkable
class ExperienceStore(Protocol):
    """Append typed events once by event_id; query only completed visible history.

    Raw query is for orchestration/evidence. ContextBuilder still produces a
    public ExperienceSummary rather than passing private events to an Evolver.
    """

    def append(self, record: ExperienceEvent) -> RecordRef: ...

    def get(self, ref: RecordRef) -> ExperienceEvent: ...

    def events(self) -> tuple[ExperienceEvent, ...]: ...

    def query(self, criteria: ExperienceQuery) -> tuple[ExperienceEvent, ...]: ...
