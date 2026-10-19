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
    def validate(self, proposal: EvolutionProposalRecord) -> ValidationBatchResult: ...
