import unittest

from plugins.validation.acceptance import ImprovementWithRegressionGuard, select_candidate
from tests.domain_helpers import report
from tests.validation.helpers import settings


class AcceptanceTests(unittest.TestCase):
    def test_positive_gain_threshold_and_no_improvement(self):
        result = report()
        for threshold, accepted, reason in [(0.5, True, "improved"), (0.51, False, "insufficient_improvement")]:
            decision = ImprovementWithRegressionGuard(settings(min_improvement=threshold)).evaluate(result)
            self.assertEqual(decision.accepted, accepted)
            self.assertEqual(decision.reason, reason)
            self.assertEqual(decision.reward, 0.5)
        unchanged = result.replace(candidate_metrics=result.baseline_metrics)
        decision = ImprovementWithRegressionGuard(settings(min_improvement=0.0)).evaluate(unchanged)
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, "no_improvement")
        self.assertEqual(decision.reward, 0)

    def test_regression_inclusive_boundary_and_positive_rejected_reward(self):
        result = report()
        # Similar improvement raises overall score despite a regression loss.
        changed = result.candidate_metrics.replace(score=0.95, regression_score=0.9)
        result = result.replace(candidate_metrics=changed)
        accepted = ImprovementWithRegressionGuard(settings(max_regression_drop=0.1)).evaluate(result)
        rejected = ImprovementWithRegressionGuard(settings(max_regression_drop=0.09)).evaluate(result)
        self.assertTrue(accepted.accepted)
        self.assertFalse(rejected.accepted)
        self.assertEqual(rejected.reason, "regression_guard")
        self.assertAlmostEqual(rejected.reward, 0.45)

    def test_incomplete_has_unknown_reward(self):
        decision = ImprovementWithRegressionGuard(settings()).evaluate(report().replace(status="incomplete", candidate_metrics=None))
        self.assertFalse(decision.accepted)
        self.assertIsNone(decision.reward)
        self.assertIsNone(decision.delta_score)

    def test_selection_joins_ids_and_uses_score_tokens_then_lexical_id(self):
        policy = ImprovementWithRegressionGuard(settings())
        a = report().replace(candidate_id="a", candidate_validation_tokens=1)
        b = report().replace(candidate_id="b", candidate_validation_tokens=2)
        self.assertEqual(select_candidate([b, a], [policy.evaluate(a), policy.evaluate(b)]), "a")
        b = b.replace(candidate_validation_tokens=1)
        self.assertEqual(select_candidate([b, a], [policy.evaluate(a), policy.evaluate(b)]), "a")
        weaker = a.replace(candidate_metrics=a.candidate_metrics.replace(score=0.75, similar_score=0.5))
        self.assertEqual(select_candidate([b, weaker], [policy.evaluate(weaker), policy.evaluate(b)]), "b")
        unchanged = a.replace(candidate_metrics=a.baseline_metrics)
        self.assertIsNone(select_candidate([unchanged], [policy.evaluate(unchanged)]))
        with self.assertRaises(ValueError):
            select_candidate([a, a], [policy.evaluate(a), policy.evaluate(a)])
        with self.assertRaises(ValueError):
            select_candidate([a], [policy.evaluate(b)])
