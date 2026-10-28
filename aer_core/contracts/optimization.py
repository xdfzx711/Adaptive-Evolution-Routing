"""Narrow capabilities for optimizers; no stable version or experience writes."""

from typing import Protocol, runtime_checkable

from aer_core.config.models import ModelProviderSettings
from aer_core.schemas.optimization import TrialObservation
from aer_core.schemas.references import AgentSnapshot
from aer_core.schemas.runtime import RunContext
from aer_core.schemas.task import TaskIdentity
from .providers import ModelProvider


@runtime_checkable
class ModelProviderFactory(Protocol):
    def create(self, settings: ModelProviderSettings) -> ModelProvider: ...


@runtime_checkable
class CandidateTrialService(Protocol):
    def tasks(self, current_task: TaskIdentity, task_ids: tuple[str, ...] | None) -> tuple[TaskIdentity, ...]: ...

    def evaluate(self, current_task: TaskIdentity, task: TaskIdentity, snapshot: AgentSnapshot,
                 prompt: str, run_ctx: RunContext) -> TrialObservation: ...
