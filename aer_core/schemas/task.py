"""Task identity and explicit separation of public and scoring-side data."""

from pydantic import Field

from .base import DomainModel, Identifier, Items, JsonObject
from .references import ArtifactRef


class TaskIdentity(DomainModel):
    benchmark_id: Identifier
    task_id: Identifier


class Task(DomainModel):
    task_id: Identifier
    input: JsonObject
    benchmark_id: Identifier
    eval_spec_ref: Identifier | None = None
    metadata: JsonObject = Field(default_factory=dict)

    @property
    def identity(self) -> TaskIdentity:
        return TaskIdentity(benchmark_id=self.benchmark_id, task_id=self.task_id)


class TaskView(DomainModel):
    task_id: Identifier
    benchmark_id: Identifier
    input: JsonObject
    public_metadata: JsonObject = Field(default_factory=dict)


class EvaluationSpec(DomainModel):
    """Private scoring-side data. Never pass this to a Router or Evolver."""

    task: TaskIdentity
    scoring_method: Identifier
    private_parameters: JsonObject
    evidence_refs: Items[ArtifactRef] = ()
