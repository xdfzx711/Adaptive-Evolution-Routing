import unittest

from aer_core.history import component_edits, limit_edits, validation_summary
from aer_core.schemas.history import ComponentEdit, ScoreChange, ValidationSummary
from aer_core.schemas.validation import LocalValidationMetrics
from tests.domain_helpers import report


class HistoryTests(unittest.TestCase):
    def test_edits_keep_complete_instructions_and_code_whitespace(self):
        instruction = "Inspect all calling paths. " * 20 + "Preserve the outermost CLI boundary."
        edits = component_edits("prompt", "Keep useful instructions.\n", "Keep useful instructions.\n" + instruction)
        self.assertEqual(len(edits), 1)
        self.assertEqual(edits[0].after, instruction)
        self.assertFalse(edits[0].truncated)
        whitespace = component_edits("code", "if ready:\n    go()\n", "if ready:\n        go()\n")
        self.assertEqual((whitespace[0].before, whitespace[0].after), ("\n    ", "\n        "))
        self.assertEqual(component_edits("prompt", instruction, instruction), ())

    def test_model_replacement_and_configured_total_edit_limit(self):
        edits = component_edits("model", "gpt-5.6-sol", "gpt-6.1-sol")
        self.assertEqual((edits[0].before, edits[0].after), ("gpt-5.6-sol", "gpt-6.1-sol"))
        clipped, truncated = limit_edits(edits, 8)
        self.assertTrue(truncated)
        self.assertTrue(clipped[0].truncated)
        self.assertEqual(sum(len(item.before) + len(item.after) for item in clipped), 8)
        self.assertEqual(ComponentEdit.model_validate_json(clipped[0].model_dump_json()), clipped[0])
        self.assertEqual(limit_edits(edits, 100), (edits, False))

    def test_score_summary_contains_before_after_and_local_regression_without_private_data(self):
        source = report().replace(local_metrics=LocalValidationMetrics(baseline_score=0.5, candidate_score=0,
            baseline_success=False, candidate_success=False, delta_score=-0.5, repaired=False))
        summary = validation_summary(source)
        self.assertEqual(summary.current_task, ScoreChange(before=0.5, after=0, delta=-0.5))
        self.assertEqual(summary.independent, ScoreChange(before=0.5, after=1, delta=0.5))
        self.assertEqual(summary.similar.delta, 1)
        self.assertEqual(summary.regression.delta, 0)
        self.assertNotIn("ref", summary.model_dump_json())
        self.assertEqual(ValidationSummary.model_validate_json(summary.model_dump_json()), summary)
