import unittest

from aer_core.schemas.evolution import EvolutionDecision, EvolutionRequest
from plugins.agents.limits import ExecutionLimits
from plugins.evolution.step_limit import StepLimitEvolver, StepLimitEvolverConfig
from tests.evolution.helpers import evolution_context


class StepLimitTests(unittest.TestCase):
    def request(self, steps):
        state, artifacts, live = evolution_context()
        ref = artifacts.put(ExecutionLimits(max_steps=steps).model_dump_json().encode(), "execution_limits")
        state = state.replace(stable_snapshot=state.stable_snapshot.replace(
            artifacts={**state.stable_snapshot.artifacts, "execution_limits": ref}), feasible_targets=["execution_limits"])
        request = EvolutionRequest(attempt_id="attempt-step", round_id=1, context=state,
            decision=EvolutionDecision(target="execution_limits", rationale="step limit reached"),
            operator_id="step_limit_evolver", max_candidates=1, run_context=live)
        return request, artifacts

    def test_increment_clamps_at_configured_cap_and_preserves_parent_components(self):
        request, artifacts = self.request(8)
        old = request.context.stable_snapshot
        result = StepLimitEvolver(StepLimitEvolverConfig(max_candidates=1, step_increment=5, max_steps=10), artifacts).evolve(request)
        self.assertEqual(result.status, "completed")
        candidate = result.candidates[0]
        self.assertEqual(set(candidate.artifact_updates), {"execution_limits"})
        self.assertEqual(ExecutionLimits.model_validate_json(artifacts.get(candidate.artifact_updates["execution_limits"])).max_steps, 10)
        self.assertEqual(ExecutionLimits.model_validate_json(artifacts.get(old.artifacts["execution_limits"])).max_steps, 8)
        self.assertEqual(result.usage.model_calls, 0)
        self.assertEqual(result.usage.total_tokens, 0)

    def test_at_or_above_cap_returns_empty_instead_of_decreasing_steps(self):
        for steps in (10, 12):
            request, artifacts = self.request(steps)
            result = StepLimitEvolver(StepLimitEvolverConfig(max_candidates=1, step_increment=2, max_steps=10), artifacts).evolve(request)
            self.assertEqual(result.status, "empty")
            self.assertFalse(result.candidates)

    def test_malformed_artifact_is_an_error_without_candidates(self):
        request, artifacts = self.request(8)
        for data in (b'{"max_steps":true}', b'{"max_steps":0}', b'{}'):
            bad = artifacts.put(data, "execution_limits")
            state = request.context.replace(stable_snapshot=request.context.stable_snapshot.replace(
                artifacts={**request.context.stable_snapshot.artifacts, "execution_limits": bad}))
            result = StepLimitEvolver(StepLimitEvolverConfig(max_candidates=1, step_increment=2, max_steps=10), artifacts).evolve(
                EvolutionRequest(attempt_id=request.attempt_id, round_id=1, context=state, decision=request.decision,
                    operator_id=request.operator_id, max_candidates=1, run_context=request.run_context))
            self.assertEqual(result.status, "error")
            self.assertFalse(result.candidates)
