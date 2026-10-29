from typing import Annotated

from pydantic import Field, model_validator

from aer_core.config.models import ComponentSettings, ModelProviderSettings, NonBlank, NonNegativeInt, PositiveFloat, PositiveInt, Settings


class GEPAPromptConfig(Settings):
    max_candidates: PositiveInt
    max_metric_calls: PositiveInt
    samples_per_evaluation: PositiveInt = 3
    max_iterations: PositiveInt = 5
    deduplicate_prompts: bool = True
    max_duplicate_retries: NonNegativeInt = 1
    reflection_minibatch_size: PositiveInt = 1
    use_merge: bool = False
    max_merge_invocations: PositiveInt = 5
    merge_val_overlap_floor: PositiveInt = 5
    skip_perfect_score: bool = False
    timeout_seconds: PositiveFloat = 600.0
    reflection_timeout_seconds: PositiveFloat = 60.0
    reflection_max_output_tokens: PositiveInt = 4096
    trial_timeout_seconds: PositiveFloat = 300.0
    train_task_ids: Annotated[list[NonBlank], Field(min_length=1)] | None = None
    selection_task_ids: Annotated[list[NonBlank], Field(min_length=1)] | None = None
    trial_evaluator: ComponentSettings | None = None
    reflection_model: ModelProviderSettings = Field(default_factory=lambda: ModelProviderSettings(
        plugin="qwen_model", model_id="${GEPA_REFLECTION_MODEL}", config={
            "api_key_env": "GEPA_REFLECTION_API_KEY", "base_url_env": "GEPA_REFLECTION_BASE_URL"}))

    @model_validator(mode="after")
    def unique_task_ids(self):
        for name in ("train_task_ids", "selection_task_ids"):
            ids = getattr(self, name)
            if ids is not None and len(ids) != len(set(ids)):
                raise ValueError(name + " must not contain duplicate task IDs")
        return self
