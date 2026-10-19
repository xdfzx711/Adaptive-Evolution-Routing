"""The explicit YAML field contract from AER_V0_1_SPEC.md."""

from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, JsonValue


def _not_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("must contain non-whitespace characters")
    return value


def _unique(values: list[str]) -> list[str]:
    if len(values) != len(set(values)):
        raise ValueError("must not contain duplicate values")
    return values


NonBlank = Annotated[str, AfterValidator(_not_blank)]
UniqueStrings = Annotated[list[NonBlank], AfterValidator(_unique)]
PositiveInt = Annotated[int, Field(gt=0)]
NonNegativeInt = Annotated[int, Field(ge=0)]
PositiveFloat = Annotated[float, Field(gt=0, allow_inf_nan=False)]
UnitFloat = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class ExperimentSettings(Settings):
    id: NonBlank
    seed: NonNegativeInt
    runner: NonBlank


class BenchmarkSettings(Settings):
    plugin: NonBlank
    execution_split: Literal["dev", "test"]
    task_ids: Annotated[UniqueStrings, Field(min_length=1)] | None


class StateSettings(Settings):
    task_mode: Literal["independent", "continual"]


class AgentSettings(Settings):
    plugin: NonBlank
    initial_prompt: NonBlank | None
    initial_snapshot_ref: NonBlank | None
    max_steps: PositiveInt


class ModelProviderSettings(Settings):
    plugin: NonBlank
    model_id: NonBlank
    config: dict[str, JsonValue]


class ToolProviderSettings(Settings):
    plugin: NonBlank
    enabled: UniqueStrings


class ProviderSettings(Settings):
    model: ModelProviderSettings
    tools: ToolProviderSettings


class AssessmentSettings(Settings):
    evaluator: NonBlank
    diagnoser: NonBlank


class EvolutionSettings(Settings):
    mode: Literal["disabled", "fixed", "manual", "llm"]
    max_candidates_per_round: PositiveInt


class ManualDecision(Settings):
    round_id: PositiveInt
    target: NonBlank
    rationale: NonBlank


class RoutingSettings(Settings):
    plugin: NonBlank | None
    fixed_target: NonBlank | None
    manual_decisions: list[ManualDecision]


class TargetSettings(Settings):
    allowed: UniqueStrings
    default_operators: dict[NonBlank, NonBlank]
    operator_configs: dict[NonBlank, dict[str, JsonValue]] = Field(default_factory=dict)


class ValidationSettings(Settings):
    plugin: NonBlank
    split: NonBlank
    group_field: NonBlank
    similar_group: NonBlank
    regression_group: NonBlank
    acceptance_policy: NonBlank
    policy_version: NonBlank
    min_improvement: UnitFloat
    max_regression_drop: UnitFloat


class FeedbackSettings(Settings):
    policy: Literal["dev_feedback_and_validation_summary"]


class BudgetLimits(Settings):
    max_evolution_rounds: NonNegativeInt
    max_tokens: PositiveInt
    max_tool_calls: NonNegativeInt
    max_elapsed_seconds: PositiveFloat
    model_call_timeout_seconds: PositiveFloat
    tool_call_timeout_seconds: PositiveFloat
    max_output_tokens_per_call: PositiveInt
    max_consecutive_no_commit_rounds: PositiveInt | None


class StorageSettings(Settings):
    root: NonBlank
    experience: NonBlank
    artifacts: NonBlank
    versions: NonBlank


class ExperimentConfig(Settings):
    schema_version: Literal["aer.v0.1"]
    experiment: ExperimentSettings
    benchmark: BenchmarkSettings
    state: StateSettings
    agent: AgentSettings
    providers: ProviderSettings
    assessment: AssessmentSettings
    evolution: EvolutionSettings
    routing: RoutingSettings
    targets: TargetSettings
    validation: ValidationSettings
    feedback: FeedbackSettings
    limits: BudgetLimits
    storage: StorageSettings
