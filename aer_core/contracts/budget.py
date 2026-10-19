"""One episode's live ledger; reserve/settle must account each operation once."""

from typing import Protocol, runtime_checkable

from aer_core.schemas.states import RunStage
from aer_core.schemas.usage import BudgetRemaining, Reservation, UsageReport


@runtime_checkable
class BudgetHandle(Protocol):
    """Raise BudgetExceeded for insufficient resource or time allowance.

    Repeated reservation or settlement of an operation must not charge twice.
    Unknown usage remains pending. The implementation is supplied in step 6.
    """

    def remaining(self) -> BudgetRemaining: ...

    def reserve(
        self, operation_id: str, stage: RunStage, *, tokens: int, tool_calls: int
    ) -> Reservation: ...

    def settle(self, operation_id: str, usage: UsageReport) -> Reservation: ...
