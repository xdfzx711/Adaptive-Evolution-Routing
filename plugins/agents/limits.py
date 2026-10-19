"""Versioned ReAct execution limits, independent of experiment bootstrap settings."""

from aer_core.schemas.base import DomainModel, PositiveInt


class ExecutionLimits(DomainModel):
    max_steps: PositiveInt
