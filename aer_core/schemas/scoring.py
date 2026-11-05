"""Public scorer semantics; no labels, reference answers or executable expressions."""

from typing import Literal
from pydantic import Field

from .base import DomainModel, FiniteFloat, Identifier, Items, ReadOnlyMap


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
