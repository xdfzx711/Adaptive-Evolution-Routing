import unittest

from pydantic import ValidationError

from aer_core.schemas.acceptance import AcceptanceWeights
from aer_core.schemas.validation import AcceptanceResult, LocalValidationMetrics, ValidationMetrics
from plugins.validation.acceptance import WeightedImprovement, select_candidate
from tests.domain_helpers import report
from tests.validation.helpers import settings


def weighted_settings(**updates):
    return settings(acceptance_policy="weighted_improvement", max_regression_drop=None, **updates)


def scored_report(local_before, local_after, similar_before=0, similar_after=0, regression_before=1, regression_after=1):
    def metrics(similar, regression):
        return ValidationMetrics(score=(similar + regression) / 2, similar_score=similar, regression_score=regression,
            success_rate=0, regression_failure_count=0, task_count=2, similar_count=1, regression_count=1)
    return report().replace(baseline_metrics=metrics(similar_before, regression_before),
        candidate_metrics=metrics(similar_after, regression_after),
        local_metrics=LocalValidationMetrics(baseline_score=local_before, candidate_score=local_after,
            baseline_success=local_before == 1, candidate_success=local_after == 1,
            delta_score=local_after - local_before, repaired=local_before != 1 and local_after == 1))


class WeightedAcceptanceTests(unittest.TestCase):
    def test_observed_requests_and_pylint_tradeoffs(self):
        policy = WeightedImprovement(weighted_settings())
        cases = [
            (scored_report(0.4666666667, 0.6, 0.1333333333, 0.1333333333), 0.06666666665),
            (scored_report(0.4666666667, 0.4666666667, 0.1333333333, 0.84), 0.176666666675),
            (scored_report(0, 0.52, 0.4076190476, 0.30325581395, 0.3777777778, 0.1866666667), 0.1861314138125),
        ]
        for result, expected in cases:
            with self.subTest(expected=expected):
                acceptance = policy.evaluate(result)
                self.assertTrue(acceptance.accepted)
                self.assertAlmostEqual(acceptance.reward, expected)
                self.assertAlmostEqual(acceptance.delta_current_task_score, result.local_metrics.delta_score)
                self.assertEqual(AcceptanceResult.model_validate_json(acceptance.model_dump_json()), acceptance)
        self.assertLess(policy.evaluate(cases[-1][0]).delta_score, 0)

    def test_threshold_zero_and_negative_gains(self):
        policy = WeightedImprovement(weighted_settings(min_improvement=0.01))
        for local_after, accepted, reason in [(0.02, True, "improved"), (0.019, False, "insufficient_improvement"),
                                               (0, False, "no_improvement")]:
            decision = policy.evaluate(scored_report(0, local_after))
            self.assertEqual((decision.accepted, decision.reason), (accepted, reason))
        self.assertFalse(policy.evaluate(scored_report(0.5, 0)).accepted)
        self.assertFalse(WeightedImprovement(weighted_settings(min_improvement=0)).evaluate(scored_report(0, 0)).accepted)

    def test_optional_regression_guard_and_configurable_weights(self):
        result = scored_report(0, 1, 0, 0, 1, 0.8)
        self.assertTrue(WeightedImprovement(weighted_settings()).evaluate(result).accepted)
        guarded = settings(acceptance_policy="weighted_improvement", max_regression_drop=0.1)
        self.assertEqual(WeightedImprovement(guarded).evaluate(result).reason, "regression_guard")
        boundary = settings(acceptance_policy="weighted_improvement", max_regression_drop=0.2)
        self.assertTrue(WeightedImprovement(boundary).evaluate(result).accepted)
        weighted = weighted_settings(weights={"current_task": 0.8, "similar": 0.1, "regression": 0.1})
        self.assertAlmostEqual(WeightedImprovement(weighted).evaluate(result).reward, 0.78)

    def test_invalid_weights_fail_configuration(self):
        for weights in ({"current_task": 0.7, "similar": 0.25, "regression": 0.25},
                        {"current_task": -0.1, "similar": 0.6, "regression": 0.5},
                        {"current_task": float("nan"), "similar": 0.25, "regression": 0.25}):
            with self.assertRaises(ValidationError):
                weighted_settings(weights=weights)
        self.assertEqual(AcceptanceWeights().model_dump(), {"current_task": 0.5, "similar": 0.25, "regression": 0.25})

    def test_missing_local_or_incomplete_validation_cannot_be_accepted(self):
        policy = WeightedImprovement(weighted_settings())
        for result, reason in [(report(), "missing_local_metrics"),
                               (scored_report(0, 1).replace(status="incomplete"), "validation_incomplete")]:
            decision = policy.evaluate(result)
            self.assertFalse(decision.accepted)
            self.assertEqual(decision.reason, reason)
            self.assertIsNone(decision.reward)
            self.assertIsNone(decision.weighted_gain)

    def test_selection_uses_weighted_reward_instead_of_independent_score(self):
        policy = WeightedImprovement(weighted_settings())
        transfer = scored_report(0, 0, 0, 0.8).replace(candidate_id="transfer")
        repair = scored_report(0, 0.6).replace(candidate_id="repair", candidate_validation_tokens=2)
        self.assertGreater(transfer.candidate_metrics.score, repair.candidate_metrics.score)
        self.assertEqual(select_candidate([transfer, repair], [policy.evaluate(repair), policy.evaluate(transfer)]), "repair")
        tied = repair.replace(candidate_id="cheap", candidate_validation_tokens=1)
        self.assertEqual(select_candidate([repair, tied], [policy.evaluate(repair), policy.evaluate(tied)]), "cheap")

    def test_breakdown_cannot_disagree_with_observed_gains(self):
        decision = WeightedImprovement(weighted_settings()).evaluate(scored_report(0, 0.52, 0.4, 0.3, 0.4, 0.2))
        with self.assertRaises(ValidationError):
            decision.replace(reward=0.9)
        with self.assertRaises(ValidationError):
            decision.replace(delta_current_task_score=1)
        with self.assertRaises(ValidationError):
            decision.replace(weighted_gain=None)
