from typing import Protocol, runtime_checkable

from aer_core.schemas.evolution import EvolutionCandidate
from aer_core.schemas.references import AgentSnapshot
from aer_core.schemas.runtime import RunContext
from aer_core.schemas.validation import AcceptanceResult, ValidationPlan, ValidationReport
from aer_core.schemas.paired import ScoredExecution
from aer_core.schemas.task import Task


@runtime_checkable
class ValidationTaskExecutor(Protocol):
    def execute(self, task: Task, snapshot: AgentSnapshot, run_ctx: RunContext) -> ScoredExecution: ...


@runtime_checkable
class ValidationCache(Protocol):
    def get(self, key: str) -> ScoredExecution | None: ...

    def put(self, key: str, result: ScoredExecution) -> None: ...


@runtime_checkable
class ValidationPlugin(Protocol):
    """Use fresh sessions with paired tasks/seeds; return reports linked by ID.

    Candidates must have candidate_version_id before validation. Partial
    results are incomplete, infrastructure failures raise ValidationError;
    the stable pointer must not change here.
    """

    def validate(
        self, stable: AgentSnapshot, candidates: tuple[EvolutionCandidate, ...],
        plan: ValidationPlan, run_ctx: RunContext,
    ) -> tuple[ValidationReport, ...]: ...


@runtime_checkable
class AcceptancePolicy(Protocol):
    """Configured policy evaluates reports without executing or committing."""

    def evaluate(self, report: ValidationReport) -> AcceptanceResult: ...
