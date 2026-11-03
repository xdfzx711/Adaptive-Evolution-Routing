"""Explicit disclosure declarations, separate from execution/scoring schemas."""

from pydantic import Field

from .base import DomainModel, Identifier, Items, NonNegativeInt, PositiveInt, ReadOnlyMap
from .task import TaskIdentity


class TaskFeedbackPolicy(DomainModel):
    task: TaskIdentity
    output_fields: Items[Identifier]
    metadata_fields: Items[Identifier] = ()
    allow_evolution: bool = False
    reason_messages: ReadOnlyMap[str] = Field(default_factory=dict)


class ContextPolicy(DomainModel):
    evaluator_id: Identifier | None = None
    task: TaskIdentity | None = None
    metadata_fields: Items[Identifier] = ("category", "source_id", "split")
    output_fields: Items[Identifier]
    artifact_kinds: Items[Identifier] = ("prompt",)
    tool_arguments: ReadOnlyMap[Items[Identifier]] = Field(default_factory=dict)
    tool_outputs: ReadOnlyMap[Items[Identifier]] = Field(default_factory=dict)
    state_fields: Items[Identifier] = ("agent_plugin", "model_steps", "termination_reason", "fresh_session")
    snapshot_config_fields: Items[Identifier] = ("max_steps",)
    allow_evolution: bool = False
    reason_messages: ReadOnlyMap[str] = Field(default_factory=dict)
    history_max_attempts: NonNegativeInt = 5
    history_max_text_chars: PositiveInt = 320
    history_max_change_chars: PositiveInt = 4000
    history_max_construction_chars: PositiveInt = 16000
