"""Compose public scorer descriptions once for diagnosis, evolution and trials."""

from aer_core.errors import ContractError
from aer_core.schemas.scoring import ScoringFinding, ScoringRules


def pointer_token(value):
    return value.replace("~", "~0").replace("/", "~1")


def scorer_feedback_policy(evaluator, evaluator_id=None, *, assessment_version=None):
    if assessment_version is not None and assessment_version != evaluator.scorer_version:
        # Standalone presentation can receive a result from an alternate scorer.
        # Never attach the configured scorer's rules/metric disclosure to it.
        return {"feedback_version": "aer.feedback.v2", "include_metrics": False,
                "scoring_rules": ScoringRules(version="aer.scoring-summary.v1", scorer_version=assessment_version,
                    description="Public scoring details are unavailable for this assessment version; configured evaluator rules do not apply.",
                    completeness="summary_only")}
    describe = getattr(evaluator, "public_scoring_rules", None)
    rules = describe() if callable(describe) else ScoringRules(
        version="aer.scoring-summary.v1", scorer_version=evaluator.scorer_version,
        description=evaluator.score_policy, completeness="summary_only")
    if not isinstance(rules, ScoringRules) or rules.scorer_version != evaluator.scorer_version:
        raise ContractError("Scoring rules must describe the active scorer version", stage="setup")
    return {"feedback_version": "aer.feedback.v2", "include_metrics": True,
            "metric_fields": tuple(dict.fromkeys((*rules.metric_definitions,
                *getattr(evaluator, "public_metric_fields", ())))),
            "evaluator_id": evaluator_id or getattr(evaluator, "plugin_id", None),
            "scoring_rules": rules,
            "verbatim_evaluator_feedback": getattr(evaluator, "public_feedback_text", False) is True}


def scoring_findings(metrics, rules):
    if rules is None:
        return ()
    compare = {"eq": lambda a, b: a == b, "lt": lambda a, b: a < b, "gt": lambda a, b: a > b}
    findings = []
    for index, rule in enumerate(rules.interpretations):
        if rule.conditions and all(c.metric in metrics and compare[c.operator](metrics[c.metric], c.value)
                                   for c in rule.conditions):
            findings.append(ScoringFinding(code=rule.code, text=rule.text, evidence_refs=[
                *[{"source": "evaluation", "path": "/metrics/" + pointer_token(c.metric)} for c in rule.conditions],
                {"source": "scoring_rules", "path": f"/interpretations/{index}"}]))
    return tuple(findings)
