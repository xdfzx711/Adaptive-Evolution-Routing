"""Optional, explicit metric contracts. These declarations are not LLM judgments."""

from typing import Literal
from pydantic import Field, model_validator

from .base import DomainModel, FiniteFloat, Identifier, Items, ReadOnlyMap


class MetricDefinition(DomainModel):
    name: Identifier
    direction: Literal["higher", "lower", "unknown"] = "unknown"
    minimum: FiniteFloat | None = None
    maximum: FiniteFloat | None = None
    kind: Literal["binary", "discrete", "continuous", "unknown"] = "unknown"
    aggregation: Literal["mean", "ratio_of_sums", "unknown"] = "unknown"
    numerator: Identifier | None = None
    denominator: Identifier | None = None

    @model_validator(mode="after")
    def valid_definition(self):
        if self.minimum is not None and self.maximum is not None and self.minimum >= self.maximum:
            raise ValueError("metric minimum must be less than maximum")
        if self.aggregation == "ratio_of_sums" and (self.numerator is None or self.denominator is None):
            raise ValueError("ratio_of_sums requires numerator and denominator metrics")
        if self.kind == "binary" and (self.minimum, self.maximum) != (0, 1):
            raise ValueError("binary metrics must declare range [0, 1]")
        return self


class ScoreTransform(DomainModel):
    type: Literal["identity", "fixed_range"] = "identity"
    minimum: FiniteFloat | None = None
    maximum: FiniteFloat | None = None

    @model_validator(mode="after")
    def valid_bounds(self):
        if self.type == "identity" and (self.minimum is not None or self.maximum is not None):
            raise ValueError("identity does not accept range overrides")
        if (self.minimum is None) != (self.maximum is None):
            raise ValueError("both fixed range bounds must be supplied together")
        if self.minimum is not None and self.minimum >= self.maximum:
            raise ValueError("transform minimum must be less than maximum")
        return self


class ScoringSettings(DomainModel):
    metric: Identifier
    transform: ScoreTransform = ScoreTransform()


class ScoreDetails(DomainModel):
    metric: Identifier
    raw_score: FiniteFloat
    scoring_version: Identifier
    transform: ScoreTransform


class MetricCondition(DomainModel):
    metric: Identifier
    operator: Literal["eq", "lt", "gt"] = "eq"
    value: FiniteFloat


class MetricInterpretation(DomainModel):
    code: Identifier
    conditions: Items[MetricCondition]
    text: Identifier


class ScoringRules(DomainModel):
    version: Identifier
    scorer_version: Identifier
    description: str
    primary_metric: Identifier | None = None
    metric_definitions: ReadOnlyMap[str] = Field(default_factory=dict)
    rules: ReadOnlyMap[str] = Field(default_factory=dict)
    interpretations: Items[MetricInterpretation] = ()
    completeness: Literal["documented", "summary_only"] = "documented"


class EvidenceReference(DomainModel):
    source: Literal["task", "execution", "evaluation", "scoring_rules"]
    path: str = Field(pattern=r"^/")


class ScoringFinding(DomainModel):
    code: Identifier
    text: Identifier
    evidence_refs: Items[EvidenceReference]
