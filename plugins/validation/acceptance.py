"""Pure configured acceptance and deterministic multi-candidate selection."""

from decimal import Decimal

from aer_core.config.models import ValidationSettings
from aer_core.schemas.acceptance import WeightedGain
from aer_core.schemas.validation import AcceptanceResult, ValidationReport


class ImprovementWithRegressionGuard:
    plugin_id = "improvement_with_regression_guard"

    def __init__(self, config: ValidationSettings):
        if config.acceptance_policy != self.plugin_id or config.policy_version != "v0.1":
            raise ValueError("unsupported acceptance policy/version")
        self.config = config

    def evaluate(self, report: ValidationReport) -> AcceptanceResult:
        base = dict(candidate_id=report.candidate_id, policy_id=self.plugin_id, policy_version=self.config.policy_version)
        if report.status != "complete":
            return AcceptanceResult(**base, accepted=False, reward=None, delta_score=None,
                delta_similar_score=None, delta_regression_score=None,
                reason="validation_error" if report.status == "error" else "validation_incomplete")
        baseline, candidate = report.baseline_metrics, report.candidate_metrics
        # Decimal comparisons preserve configured inclusive threshold boundaries.
        delta = Decimal(str(candidate.score)) - Decimal(str(baseline.score))
        similar = Decimal(str(candidate.similar_score)) - Decimal(str(baseline.similar_score))
        regression = Decimal(str(candidate.regression_score)) - Decimal(str(baseline.regression_score))
        reason = ("no_improvement" if delta <= 0 else
                  "insufficient_improvement" if delta < Decimal(str(self.config.min_improvement)) else
                  "regression_guard" if regression < -Decimal(str(self.config.max_regression_drop)) else "improved")
        return AcceptanceResult(**base, accepted=reason == "improved", reward=float(delta), reason=reason,
            delta_score=float(delta), delta_similar_score=float(similar), delta_regression_score=float(regression),
            delta_current_task_score=report.local_metrics.delta_score if report.local_metrics else None)


class WeightedImprovement:
    plugin_id = "weighted_improvement"

    def __init__(self, config: ValidationSettings):
        if config.acceptance_policy != self.plugin_id or config.policy_version != "v0.1":
            raise ValueError("unsupported acceptance policy/version")
        self.config = config

    def evaluate(self, report: ValidationReport) -> AcceptanceResult:
        base = dict(candidate_id=report.candidate_id, policy_id=self.plugin_id, policy_version=self.config.policy_version)
        if report.status != "complete" or report.local_metrics is None:
            reason = ("validation_error" if report.status == "error" else "validation_incomplete"
                      if report.status != "complete" else "missing_local_metrics")
            return AcceptanceResult(**base, accepted=False, reward=None, delta_score=None,
                delta_similar_score=None, delta_regression_score=None, reason=reason)
        baseline, candidate = report.baseline_metrics, report.candidate_metrics
        delta = Decimal(str(candidate.score)) - Decimal(str(baseline.score))
        similar = Decimal(str(candidate.similar_score)) - Decimal(str(baseline.similar_score))
        regression = Decimal(str(candidate.regression_score)) - Decimal(str(baseline.regression_score))
        local = Decimal(str(report.local_metrics.candidate_score)) - Decimal(str(report.local_metrics.baseline_score))
        weights = self.config.weights
        contributions = {name: Decimal(str(getattr(weights, name))) * gain
                         for name, gain in (("current_task", local), ("similar", similar), ("regression", regression))}
        total = sum(contributions.values(), Decimal(0))
        guard = self.config.max_regression_drop
        reason = ("no_improvement" if total <= 0 else
                  "insufficient_improvement" if total < Decimal(str(self.config.min_improvement)) else
                  "regression_guard" if guard is not None and regression < -Decimal(str(guard)) else "improved")
        return AcceptanceResult(**base, accepted=reason == "improved", reward=float(total), reason=reason,
            delta_score=float(delta), delta_similar_score=float(similar), delta_regression_score=float(regression),
            delta_current_task_score=float(local), weighted_gain=WeightedGain(weights=weights,
                **{name: float(value) for name, value in contributions.items()}, total=float(total)))


def select_candidate(reports, acceptances) -> str | None:
    reports, acceptances = tuple(reports), tuple(acceptances)
    by_id = {report.candidate_id: report for report in reports}
    policies = {result.candidate_id: result for result in acceptances}
    if len(by_id) != len(reports) or len(policies) != len(acceptances) or set(by_id) != set(policies):
        raise ValueError("selection requires unique, matching candidate IDs")
    passed = [by_id[key] for key, policy in policies.items() if policy.accepted]
    if any(report.status != "complete" for report in passed):
        raise ValueError("accepted candidate requires complete validation")
    if not passed:
        return None
    return min(passed, key=lambda report: (-policies[report.candidate_id].reward
        if policies[report.candidate_id].weighted_gain is not None else -report.candidate_metrics.score,
        report.candidate_validation_tokens, report.candidate_id)).candidate_id
