"""HotpotQA native metrics, using the pinned official evaluation functions."""

from hashlib import sha256
import json
from time import monotonic
from typing import Literal

from aer_core.config.models import Settings
from aer_core.errors import EvaluationError
from aer_core.identifiers import timestamp_id
from aer_core.schemas.execution import EvaluationResult
from aer_core.schemas.usage import ResourceUsage
from .hotpotqa_official.evaluate import update_answer, update_sp


UPSTREAM_COMMIT = "3635853403a8735609ee997664e1528f4480762a"
METRICS = ("em", "f1", "prec", "recall", "sp_em", "sp_f1", "sp_prec", "sp_recall",
           "joint_em", "joint_f1", "joint_prec", "joint_recall")


class HotpotQAEvaluatorSettings(Settings):
    primary_metric: Literal["joint_f1", "f1", "em", "sp_f1", "joint_em"] = "joint_f1"


def valid_supporting_facts(value):
    return isinstance(value, (list, tuple)) and all(
        isinstance(item, (list, tuple)) and len(item) == 2 and isinstance(item[0], str)
        and type(item[1]) is int for item in value)


def score_prediction(output, answer, supporting_facts):
    """Official missing-prediction semantics; malformed fields count as missing.

    Unexpected but well-typed titles/indices are scored as false positives, not
    removed to improve a prediction. Duplicate facts use the official set rule.
    """
    metrics = dict.fromkeys(METRICS, 0.0)
    predicted_answer = output.get("answer")
    predicted_facts = output.get("supporting_facts")
    valid_answer = isinstance(predicted_answer, str)
    valid_facts = valid_supporting_facts(predicted_facts)
    if valid_answer:
        em, precision, recall = update_answer(metrics, predicted_answer, answer)
    if valid_facts:
        sp_em, sp_precision, sp_recall = update_sp(metrics, predicted_facts, supporting_facts)
    if valid_answer and valid_facts:
        jp, jr = precision * sp_precision, recall * sp_recall
        metrics.update(joint_em=float(em * sp_em), joint_prec=jp, joint_recall=jr,
                       joint_f1=2 * jp * jr / (jp + jr) if jp + jr else 0.0)
    return metrics, valid_answer and valid_facts


class HotpotQAEvaluator:
    plugin_id = "hotpotqa_evaluator"

    def __init__(self, settings):
        self.settings = settings
        fingerprint = sha256(json.dumps(settings.model_dump(), sort_keys=True).encode()).hexdigest()[:12]
        self.scorer_version = f"hotpotqa.official.{UPSTREAM_COMMIT[:12]}.v1.{fingerprint}"
        self.score_policy = f"Official HotpotQA {settings.primary_metric} (0..1); success iff joint_em=1"

    def evaluate(self, task, result, spec, run_ctx):
        start = monotonic()
        if (spec.task != task.identity or result.task_id != task.task_id
                or spec.scoring_method != "hotpotqa.official.v1"
                or result.status not in {"completed", "step_limit"}):
            raise EvaluationError("HotpotQA requires matching task/execution and native scoring spec", stage="evaluation")
        try:
            metrics, valid = score_prediction(result.output, spec.private_parameters["answer"],
                                              spec.private_parameters["supporting_facts"])
        except (KeyError, TypeError, ValueError) as error:
            raise EvaluationError("Invalid HotpotQA private scoring specification", stage="evaluation") from error
        success = metrics["joint_em"] == 1.0
        reason = ("correct" if success else "invalid_output_format" if not valid else
                  "incorrect_supporting_facts" if metrics["em"] == 1.0 else "incorrect_answer")
        return EvaluationResult(evaluation_id=timestamp_id("evaluation"), task_id=task.task_id,
            agent_version_id=result.agent_version_id, execution_operation_id=result.operation_id,
            operation_id=run_ctx.operation_id, score=metrics[self.settings.primary_metric], success=success,
            metrics=metrics, feedback="Official HotpotQA evaluation: " + reason, reason_code=reason,
            assessment_version=self.scorer_version,
            usage=ResourceUsage.zero().replace(elapsed_seconds=monotonic() - start))
