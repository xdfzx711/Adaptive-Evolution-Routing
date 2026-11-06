"""Public scoring protocol and metric aggregation, independent of benchmark names."""

from fractions import Fraction
from functools import reduce
from math import gcd, lcm
from statistics import fmean

from aer_core.contracts.evaluator import MetricDefinitionProvider


def scoring_protocol(prepared):
    evaluator, config = prepared.evaluator, prepared.loaded_config.config
    if not hasattr(evaluator, "protocol"):
        return None  # Preserve legacy protocol and cache conditions exactly.
    groups = ({tuple(map(len, prepared.validation_groups(task))) for task in prepared.execution_tasks}
              if config.evolution.mode != "disabled" else set())
    profile = dict(evaluator.protocol)
    profile["metric_definitions"] = [item.model_dump(mode="json") for item in evaluator.metric_definitions()]
    profile["optimization_aggregation"] = "arithmetic mean of per-task optimization scores"
    profile["outer_sampling"] = {"executions_per_task_version_seed": 1,
                                  "validation_groups": [dict(similar=a, regression=b) for a, b in sorted(groups)]}
    profile["inner_sampling"] = {target: {"samples_per_evaluation": settings.get("samples_per_evaluation", 3),
            "aggregation": "arithmetic mean of sampled optimization scores"}
        for target, settings in config.targets.operator_configs.items()
        if config.targets.default_operators.get(target) == "gepa_prompt_evolver"}
    interpretation = {"configured_min_gain": config.validation.min_improvement,
                      "configured_max_regression_drop": config.validation.max_regression_drop,
                      "automatic_threshold_adjustment": False}
    # Only infer a lattice when its assumptions are known. This is not a promise
    # that a candidate can attain the next lattice point from the current state.
    if (evaluator.metric.kind == "binary" and evaluator.transform.type == "identity"
            and config.validation.acceptance_policy == "weighted_improvement"):
        weights = config.validation.weights
        lattice = []
        for similar_count, regression_count in sorted(groups):
            steps = [Fraction(str(weights.current_task)), Fraction(str(weights.similar)) / similar_count,
                     Fraction(str(weights.regression)) / regression_count]
            denominator = lcm(*(step.denominator for step in steps))
            numerator = reduce(gcd, (step.numerator * (denominator // step.denominator) for step in steps))
            step = Fraction(numerator, denominator)
            threshold = Fraction(str(config.validation.min_improvement))
            ratio = threshold / step
            minimum = max(1, -(-ratio.numerator // ratio.denominator)) * step
            guard = config.validation.max_regression_drop
            allowed = None if guard is None else Fraction(str(guard)) * regression_count
            lattice.append({"similar_count": similar_count, "regression_count": regression_count,
                "weighted_gain_lattice_step": float(step), "minimum_positive_lattice_gain_meeting_threshold": float(minimum),
                "max_regression_net_success_loss": None if allowed is None else allowed.numerator // allowed.denominator})
        interpretation["binary_lattice"] = lattice
        interpretation["note"] = "Lattice bounds only; attainable gains also depend on baseline outcomes and regression guard."
    profile["threshold_interpretation"] = interpretation
    return profile


def aggregate_declared_metrics(evaluations, evaluator):
    """Official ratio metrics pool counts; optimization still averages task scores."""
    if not isinstance(evaluator, MetricDefinitionProvider):
        return {}
    rows = [row["metrics"] for row in evaluations if row is not None]
    result = {}
    for metric in evaluator.metric_definitions():
        if not rows or any(metric.name not in row for row in rows):
            continue
        if metric.aggregation == "mean":
            result[metric.name] = fmean(row[metric.name] for row in rows)
        elif metric.aggregation == "ratio_of_sums":
            if any(metric.numerator not in row or metric.denominator not in row for row in rows):
                continue
            denominator = sum(row[metric.denominator] for row in rows)
            result[metric.name] = sum(row[metric.numerator] for row in rows) / denominator if denominator else None
    return result
