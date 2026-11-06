"""Deterministic reference-edit similarity; never executes tests or containers."""

from time import monotonic

from aer_core.config.models import Settings, UnitFloat
from aer_core.errors import EvaluationError
from aer_core.identifiers import timestamp_id
from aer_core.schemas.execution import EvaluationResult
from aer_core.schemas.usage import ResourceUsage
from plugins.workspace.patches import patch_changes


class StaticPatchSettings(Settings):
    changed_lines_weight: UnitFloat = 0.8
    success_threshold: UnitFloat = 1.0


class StaticPatchEvaluator:
    scorer_version = "static.patch.v1"
    score_policy = "static proxy: weighted changed-line F1 and changed-file F1; not test resolution"
    public_reason_messages = {
        "correct": "Static comparison met the threshold. This does not establish test correctness.",
    }

    def __init__(self, settings):
        self.settings = settings

    def public_scoring_rules(self):
        from .scoring_rules import static_patch_rules
        return static_patch_rules(self)

    def evaluate(self, task, result, spec, run_ctx):
        begin = monotonic()
        if (spec.task != task.identity or result.task_id != task.task_id or spec.scoring_method != self.scorer_version
                or result.status not in {"completed", "step_limit"}):
            raise EvaluationError("Static scorer requires matching task/execution and static.patch.v1 spec",
                                  stage="evaluation", plugin_id="static_patch_evaluator")
        try:
            expected, expected_files = patch_changes(spec.private_parameters["reference_patch"])
            if not expected:
                raise ValueError("reference patch must contain textual changes")
        except (KeyError, ValueError, TypeError) as error:
            raise EvaluationError("Invalid private reference patch", stage="evaluation",
                                  plugin_id="static_patch_evaluator") from error
        score, lines_f1, files_f1, valid = 0.0, 0.0, 0.0, True
        patch = result.output.get("patch")
        try:
            actual, actual_files = patch_changes(patch)
        except (ValueError, TypeError):
            actual, actual_files, valid = {}, set(), False
        syntax_valid = result.output.get("syntax_valid") is True
        if not valid or result.output.get("patch_valid") is not True:
            reason = "invalid_patch"
        elif not actual:
            reason = "empty_patch"
        elif not syntax_valid:
            reason = "syntax_error"
        else:
            overlap = sum((actual & expected).values())
            lines_f1 = 2 * overlap / (sum(actual.values()) + sum(expected.values()))
            files_f1 = 2 * len(actual_files & expected_files) / (len(actual_files) + len(expected_files))
            weight = self.settings.changed_lines_weight
            score = weight * lines_f1 + (1 - weight) * files_f1
            reason = "static_match" if score >= self.settings.success_threshold else "static_partial_match"
        success = reason == "static_match"
        return EvaluationResult(evaluation_id=timestamp_id("evaluation"), task_id=task.task_id,
            agent_version_id=result.agent_version_id, execution_operation_id=result.operation_id,
            operation_id=run_ctx.operation_id, score=score, success=success, reason_code=reason,
            feedback="Static reference-patch comparison only; no tests were executed. " + reason,
            metrics={"changed_line_f1": lines_f1, "changed_file_f1": files_f1,
                     "patch_valid": float(valid and result.output.get("patch_valid") is True),
                     "syntax_valid": float(syntax_valid), "tests_executed": 0.0},
            usage=ResourceUsage.zero().replace(elapsed_seconds=monotonic() - begin))
