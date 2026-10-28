"""Live execution context and its serializable record, without SDK objects."""

from dataclasses import dataclass

from aer_core.contracts.budget import BudgetHandle

from .base import DomainModel, Identifier, NonNegativeInt, PositiveFloat, PositiveInt
from .states import RunStage, StageValue
from .usage import BudgetRemaining


class RunContextRecord(DomainModel):
    run_id: Identifier
    episode_id: Identifier
    operation_id: Identifier
    stage: StageValue
    seed: NonNegativeInt
    workspace_ref: Identifier
    model_timeout_seconds: PositiveFloat
    tool_timeout_seconds: PositiveFloat
    max_output_tokens: PositiveInt
    remaining_budget: BudgetRemaining | None
    execution_workspace_root: Identifier | None = None
    execution_timeout_seconds: PositiveFloat | None = None


@dataclass(frozen=True)
class RunContext:
    run_id: str
    episode_id: str
    operation_id: str
    stage: RunStage
    seed: int
    budget: BudgetHandle | None
    workspace_ref: str
    model_timeout_seconds: float
    tool_timeout_seconds: float
    max_output_tokens: int
    execution_workspace_root: str | None = None
    execution_timeout_seconds: float | None = None

    def __post_init__(self):
        if self.budget is not None and not isinstance(self.budget, BudgetHandle):
            raise TypeError("budget must be None or implement BudgetHandle")
        record = self.to_record()
        for name in RunContextRecord.model_fields:
            if name != "remaining_budget":
                object.__setattr__(self, name, getattr(record, name))

    def to_record(self) -> RunContextRecord:
        return RunContextRecord(
            run_id=self.run_id, episode_id=self.episode_id,
            operation_id=self.operation_id, stage=self.stage, seed=self.seed,
            workspace_ref=self.workspace_ref,
            model_timeout_seconds=self.model_timeout_seconds,
            tool_timeout_seconds=self.tool_timeout_seconds,
            max_output_tokens=self.max_output_tokens,
            remaining_budget=self.budget.remaining() if self.budget is not None else None,
            execution_workspace_root=self.execution_workspace_root,
            execution_timeout_seconds=self.execution_timeout_seconds,
        )

    @classmethod
    def from_record(cls, record: RunContextRecord, *, budget: BudgetHandle | None):
        """Reattach the caller's live ledger; remaining budget is not restored."""
        return cls(**{
            name: getattr(record, name)
            for name in RunContextRecord.model_fields if name != "remaining_budget"
        }, budget=budget)
