"""Explicit limits and provider selections for one tool construction attempt."""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from aer_core.config.models import ModelProviderSettings, NonBlank, NonNegativeInt, PositiveFloat, PositiveInt, Settings


class OSCopilotConfig(Settings):
    max_candidates: PositiveInt = 1
    source_path: NonBlank | None = None
    generation_model: ModelProviderSettings | None = None
    judge_model: ModelProviderSettings | None = None
    max_repair_iterations: NonNegativeInt = 3
    max_replan_iterations: NonNegativeInt = 2
    max_generated_tools: PositiveInt = 3
    max_plan_nodes: PositiveInt = 12
    min_generality_score: Annotated[int, Field(ge=1, le=10)] = 8
    timeout_seconds: PositiveFloat = 300.0
    model_timeout_seconds: PositiveFloat = 60.0
    max_model_retries: NonNegativeInt = 2
    retry_backoff_seconds: Annotated[float, Field(gt=0, le=30)] = 2.0
    max_output_tokens: PositiveInt = 4096
    max_expanded_output_tokens: PositiveInt = 16384
    max_output_expansions: NonNegativeInt = 1
    max_export_metadata_repairs: NonNegativeInt = 1
    max_export_format_repairs: Annotated[int, Field(ge=0, le=1)] = 1
    export_response_format: Literal["auto", "text"] = "auto"
    max_response_format_repairs: NonNegativeInt = 1
    export_token_reserve: NonNegativeInt = 0
    max_example_chars: PositiveInt = 2048
    max_model_calls: PositiveInt = 20
    max_tokens: PositiveInt = 20000

    @model_validator(mode="after")
    def one_bundle(self):
        if self.max_candidates != 1:
            raise ValueError("OS-Copilot exports one frozen tool bundle per evolution round")
        if self.export_token_reserve >= self.max_tokens:
            raise ValueError("export_token_reserve must be smaller than max_tokens")
        return self
