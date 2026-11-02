"""Public execution evidence shared by Agent and evolution adapters."""

from typing import Literal

from .base import DomainModel, Identifier, Items, JsonObject, NonNegativeInt
from .execution import Diagnosis, FeedbackView, TraceEvent
from .task import TaskView
from .usage import ResourceUsage


class SkillReadEvidence(DomainModel):
    name: Identifier
    call_id: Identifier
    execution_operation_id: Identifier
    content_digest: Identifier
    event_seq: NonNegativeInt | None = None


class ExecutionEvidence(DomainModel):
    schema_version: Literal["aer.execution_evidence.v1"] = "aer.execution_evidence.v1"
    evidence_id: Identifier
    snapshot_version_id: Identifier
    skill_library_artifact_id: Identifier
    task: TaskView
    output: JsonObject
    usage: ResourceUsage
    events: Items[TraceEvent]
    skill_reads: Items[SkillReadEvidence] = ()
    injected_skills: Items[Identifier] = ()
    feedback: FeedbackView
    diagnosis: Diagnosis | None = None
    gaps: Items[Identifier] = ()
    granularity: Literal["visible_events"] = "visible_events"
