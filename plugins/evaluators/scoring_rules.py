"""Descriptions of existing scoring implementations. This module never grades a task."""

from aer_core.schemas.scoring import ScoringRules


def interpretation(code, text, **conditions):
    return {"code": code, "text": text, "conditions": [
        {"metric": metric, "operator": operator, "value": value}
        for metric, (operator, value) in conditions.items()]}


def hotpotqa_rules(evaluator):
    return ScoringRules(version="hotpotqa.rules.v1", scorer_version=evaluator.scorer_version,
        description=evaluator.score_policy, primary_metric=evaluator.settings.primary_metric,
        metric_definitions={
            "em": "Normalized answer exact match (0 or 1).", "f1": "Answer token F1 (0..1).",
            "prec": "Answer token precision (0..1).", "recall": "Answer token recall (0..1).",
            "sp_em": "Exact equality of supporting-fact sets (0 or 1).", "sp_f1": "Supporting-fact set F1 (0..1).",
            "sp_prec": "Fraction of predicted distinct supporting facts in the reference set (0..1).",
            "sp_recall": "Fraction of reference supporting facts recovered (0..1).",
            "joint_em": "em * sp_em (0 or 1).", "joint_prec": "prec * sp_prec (0..1).",
            "joint_recall": "recall * sp_recall (0..1).",
            "joint_f1": "Harmonic mean of joint_prec and joint_recall; zero if their sum is zero."},
        rules={
            "answer_normalization": "Lowercase, remove ASCII punctuation, remove English articles a/an/the, collapse whitespace. A trailing period alone cannot change answer EM or F1.",
            "special_answers": "If either normalized answer is yes, no or noanswer and the two normalized answers differ, answer F1/precision/recall are zero. A verbose 'Yes, ...' differs from 'yes'.",
            "answer_overlap": "Other answers use multiset token overlap after normalization; precision and recall distinguish excess from missing tokens. They do not identify the exact missing/excess span.",
            "supporting_facts": "Compare sets of exact (original title, zero-based sentence_id) pairs. Duplicate pairs count once; titles are not answer-normalized. Precision=1 means every predicted pair is correct; recall<1 means some reference pairs are missing.",
            "joint_score": "joint_prec=prec*sp_prec; joint_recall=recall*sp_recall; joint_f1=2*joint_prec*joint_recall/(joint_prec+joint_recall), or zero when the denominator is zero. Joint F1 is not generally answer F1 multiplied by supporting-fact F1.",
            "success": "success iff joint_em=1, regardless of the configured primary metric.",
            "format": "Missing or malformed answer/supporting_facts fields receive zero for the affected metrics. Well-typed incorrect titles/indices are scored as false positives, not removed.",
            "reason_priority": "correct if joint_em=1; otherwise invalid_output_format for malformed fields, incorrect_supporting_facts if em=1, else incorrect_answer. incorrect_answer alone does not determine whether supporting facts are correct.",
            "limits": "Metrics describe the submitted answer and evidence, not the agent's internal reasoning. Missing facts cannot be identified from their count or recall alone."},
        interpretations=[
            interpretation("answer_exact", "The normalized answer is correct.", em=("eq", 1.0)),
            interpretation("supporting_facts_exact", "The supporting-fact set is entirely correct and complete.", sp_em=("eq", 1.0)),
            interpretation("supporting_facts_incomplete", "All submitted supporting facts are correct, but the set is incomplete. These metrics do not identify which additional facts are missing.", sp_prec=("eq", 1.0), sp_recall=("lt", 1.0)),
            interpretation("answer_excess_tokens", "All reference answer tokens are covered, but the prediction contains excess tokens under the scorer's normalization. This does not identify the exact reference wording.", recall=("eq", 1.0), prec=("lt", 1.0)),
        ])


def deterministic_rules(evaluator):
    return ScoringRules(version="toy.rules.v1", scorer_version=evaluator.scorer_version,
        description=evaluator.score_policy, rules={
            "format": "Required output fields and types must satisfy the task's declared output format.",
            "answer": "Numeric tasks compare exact rational representations within the configured absolute tolerance; use the task's public numeric_tolerance when supplied. Structured tasks compare expected fields by equality. Reference answers remain private.",
            "success": "score=float(format_valid and answer_correct); success requires both.",
            "limits": "answer_correct=0 when the format is invalid; that alone does not establish an independent reasoning error."},
        metric_definitions={"format_valid": "Output meets the declared format (0 or 1).",
                            "answer_correct": "Answer comparison passed after format validation (0 or 1)."},
        interpretations=[interpretation("output_format_valid", "The submitted output has the required format.", format_valid=("eq", 1.0))])


def static_patch_rules(evaluator):
    settings = evaluator.settings
    return ScoringRules(version="static-patch.rules.v1", scorer_version=evaluator.scorer_version,
        description=evaluator.score_policy, metric_definitions={
            "changed_line_f1": "Changed-line multiset F1 against reference edits.",
            "changed_file_f1": "Changed-file set F1 against reference files.",
            "patch_valid": "Patch passes the format checks (0 or 1).",
            "syntax_valid": "Reported syntax validation flag (0 or 1).",
            "tests_executed": "Zero: this scorer never executes tests."},
        rules={"score": f"For nonempty valid syntactically valid patches: {settings.changed_lines_weight}*changed_line_f1 + {1-settings.changed_lines_weight}*changed_file_f1. Otherwise score is zero.",
               "success": f"A valid, nonempty, syntactically valid patch must reach score >= {settings.success_threshold}.",
               "limits": "This is a static similarity proxy. Low similarity does not prove incorrect behavior, and success does not prove tests pass. Never infer private reference edits from the score."},
        interpretations=[interpretation("tests_not_executed", "The evaluator did not execute tests; this score cannot establish test success or failure.", tests_executed=("eq", 0.0))])
