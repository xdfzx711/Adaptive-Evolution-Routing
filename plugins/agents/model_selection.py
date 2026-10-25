"""Codex execution-model catalog and adapter; never changes framework providers."""

from typing import Literal

from pydantic import Field, model_validator

from aer_core.config.models import Settings
from plugins.model_selection import ModelSelection


SUPPORTED_MODELS = ("gpt-6.1-sol", "gpt-6-sol", "gpt-6-luna", "gpt-5.6-sol")
ModelId = Literal["gpt-6.1-sol", "gpt-6-sol", "gpt-6-luna", "gpt-5.6-sol"]
ReasoningEffort = Literal["low", "medium", "high", "xhigh", "max", "ultra"]


class CodexModelSpace(Settings):
    allowed_models: list[ModelId] = Field(default_factory=lambda: list(SUPPORTED_MODELS))
    # Opt-in and per-model: do not assume that every backend supports every effort.
    allowed_reasoning_efforts: dict[ModelId, list[ReasoningEffort | None]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def valid_space(self):
        if not self.allowed_models or len(set(self.allowed_models)) != len(self.allowed_models):
            raise ValueError("allowed_models must be nonempty and unique")
        if not set(self.allowed_reasoning_efforts) <= set(self.allowed_models):
            raise ValueError("allowed_reasoning_efforts keys must belong to allowed_models")
        for efforts in self.allowed_reasoning_efforts.values():
            if not efforts or len(set(efforts)) != len(efforts):
                raise ValueError("each model's reasoning efforts must be nonempty and unique")
        return self

    def selections(self) -> tuple[ModelSelection, ...]:
        return tuple(ModelSelection(model_id=model, reasoning_effort=effort)
            for model in self.allowed_models for effort in self.allowed_reasoning_efforts.get(model, [None]))


class CodexModelAdapter:
    compatible_agents = ("codex_cli_agent",)

    def options(self, snapshot):
        if snapshot.agent_plugin_id not in self.compatible_agents:
            raise ValueError("Codex model adapter requires a Codex Agent")
        values = {key: snapshot.model_dump(mode="json")["config"][key]
                  for key in ("allowed_models", "allowed_reasoning_efforts") if key in snapshot.config}
        return CodexModelSpace.model_validate(values).selections()

    def read(self, snapshot, artifacts):
        selection = ModelSelection.model_validate_json(artifacts.get(snapshot.artifacts["model"]))
        if selection not in self.options(snapshot):
            raise ValueError("Snapshot model/reasoning combination is outside the configured catalog")
        return selection

    def encode(self, selection):
        # Legacy model-only artifacts retain their original representation.
        return selection.model_dump_json(indent=2, exclude_none=True).encode("utf-8")
