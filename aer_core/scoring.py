"""Fixed metric projection shared by optimizers and outer evaluation."""

from hashlib import sha256
import json
from math import isfinite

from aer_core.contracts.evaluator import MetricDefinitionProvider
from aer_core.errors import ContractError, EvaluationError
from aer_core.schemas.scoring import ScoreDetails, ScoreTransform, ScoringRules


class ConfiguredScoringEvaluator:
    """Preserve official metrics/success; project one metric into optimization score."""

    def __init__(self, evaluator, settings):
        if not isinstance(evaluator, MetricDefinitionProvider):
            raise ContractError("Explicit scoring requires evaluator metric declarations", stage="setup")
        definitions = evaluator.metric_definitions()
        names = [item.name for item in definitions]
        if len(names) != len(set(names)) or settings.metric not in names:
            raise ContractError("Selected scoring metric is missing or metric declarations are ambiguous", stage="setup")
        self.metric = next(item for item in definitions if item.name == settings.metric)
        metric, transform = self.metric, settings.transform
        if metric.direction == "unknown":
            raise ContractError("Selected metric must declare its optimization direction", stage="setup")
        if transform.type == "identity":
            if metric.direction != "higher" or (metric.minimum, metric.maximum) != (0, 1):
                raise ContractError("Identity scoring requires a higher-is-better metric with range [0, 1]", stage="setup")
        else:
            low = transform.minimum if transform.minimum is not None else metric.minimum
            high = transform.maximum if transform.maximum is not None else metric.maximum
            if low is None or high is None or low >= high:
                raise ContractError("Fixed-range scoring requires explicit, ordered finite bounds", stage="setup")
            transform = ScoreTransform(type="fixed_range", minimum=low, maximum=high)
        self.evaluator, self.transform = evaluator, transform
        self.success_policy = evaluator.success_policy
        self.protocol = {"schema_version": "aer.scoring.v1", "metric": metric.model_dump(mode="json"),
                         "transform": transform.model_dump(mode="json"), "success_policy": evaluator.success_policy,
                         "native_scorer_version": evaluator.scorer_version}
        fingerprint = sha256(json.dumps(self.protocol, sort_keys=True).encode()).hexdigest()[:20]
        self.scorer_version = f"{evaluator.scorer_version}.projection.v1.{fingerprint}"
        self.protocol["scorer_version"] = self.scorer_version
        self.score_policy = (f"Optimize {metric.name} via {transform.type} (0..1, higher is better); "
                             f"official success: {evaluator.success_policy}")

    def __getattr__(self, name):
        return getattr(self.evaluator, name)

    def metric_definitions(self):
        # Protocol runtime checks use static attribute lookup on Python >= 3.12.
        return self.evaluator.metric_definitions()

    def public_scoring_rules(self):
        """Keep native scoring facts while describing the active score projection."""
        describe = getattr(self.evaluator, "public_scoring_rules", None)
        native = describe() if callable(describe) else ScoringRules(
            version="aer.scoring-summary.v1", scorer_version=self.evaluator.scorer_version,
            description=self.evaluator.score_policy, completeness="summary_only")
        if not isinstance(native, ScoringRules) or native.scorer_version != self.evaluator.scorer_version:
            raise ContractError("Scoring rules must describe the native scorer version", stage="setup")
        transform = self.transform
        formula = self.metric.name
        if transform.type == "fixed_range":
            formula = f"({self.metric.name} - {transform.minimum}) / ({transform.maximum} - {transform.minimum})"
            if self.metric.direction == "lower":
                formula = f"1 - ({formula})"
        definitions = dict(native.metric_definitions)
        definitions.setdefault(self.metric.name,
            f"Native {self.metric.kind} metric; direction={self.metric.direction}; "
            f"declared range [{self.metric.minimum}, {self.metric.maximum}].")
        return native.replace(version=native.version + ".projection.v1", scorer_version=self.scorer_version,
            description=self.score_policy, primary_metric=self.metric.name, metric_definitions=definitions,
            rules={**native.rules, "optimization_score":
                f"AER score = {formula}. Native metrics and success are unchanged; "
                "this projection does not redefine the native scoring rules. No clipping is applied."})

    def evaluate(self, task, result, spec, run_ctx):
        evaluation = self.evaluator.evaluate(task, result, spec, run_ctx)
        try:
            raw = evaluation.metrics[self.metric.name]
            if not isfinite(raw):
                raise ValueError("nonfinite metric")
            if ((self.metric.minimum is not None and raw < self.metric.minimum)
                    or (self.metric.maximum is not None and raw > self.metric.maximum)):
                raise ValueError("metric outside its declared official range")
            if self.metric.kind == "binary" and raw not in (0, 1):
                raise ValueError("binary metric must be 0 or 1")
            score = raw
            if self.transform.type == "fixed_range":
                low, high = self.transform.minimum, self.transform.maximum
                if not low <= raw <= high:
                    raise ValueError("metric outside configured fixed range; no implicit clipping")
                score = (raw - low) / (high - low)
                if self.metric.direction == "lower":
                    score = 1 - score
            return evaluation.replace(score=score, assessment_version=self.scorer_version,
                score_details=ScoreDetails(metric=self.metric.name, raw_score=raw,
                    scoring_version=self.scorer_version, transform=self.transform))
        except (KeyError, ValueError, TypeError, OverflowError) as error:
            raise EvaluationError(f"Cannot project official metric {self.metric.name}: {error}",
                                  stage="evaluation", usage=evaluation.usage) from error


def configure_scoring(evaluator, settings):
    # Identity of legacy evaluators, including scorer/cache version, is preserved.
    return evaluator if settings is None else ConfiguredScoringEvaluator(evaluator, settings)
