"""Services used by the task controller; no startup, filesystem or SDK types."""

from typing import Protocol, runtime_checkable

from aer_core.schemas.assessment import TaskExecutionRecord
from aer_core.schemas.paired import ValidationBatchResult
from aer_core.schemas.proposal import EvolutionProposalRecord
from aer_core.schemas.references import AgentSnapshot


@runtime_checkable
class ProposalService(Protocol):
    def feasible_targets(self, stable: AgentSnapshot) -> tuple[str, ...]: ...

    def propose(self, record: TaskExecutionRecord, *, round_id: int) -> EvolutionProposalRecord: ...


@runtime_checkable
class CandidateValidationService(Protocol):
    """Return paired reports and candidate-ID-linked current_task_results.

    A selected candidate needs complete current-task execution evidence for
    the controller to adopt after commit, without invoking the Agent again.
    """

    def validate(self, proposal: EvolutionProposalRecord) -> ValidationBatchResult: ...
