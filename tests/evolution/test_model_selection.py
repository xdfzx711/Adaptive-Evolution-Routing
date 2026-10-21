import json
import unittest

from aer_core.schemas.evolution import EvolutionDecision, EvolutionRequest
from plugins.agents.model_selection import ModelSelection, SUPPORTED_MODELS
from plugins.evolution.model_selection import ModelSelectionEvolver, ModelSelectionEvolverConfig
from tests.evolution.helpers import evolution_context
from tests.execution.helpers import RecordingModel, response


class ModelSelectionTests(unittest.TestCase):
    def request(self, allowed=SUPPORTED_MODELS):
        state, artifacts, live = evolution_context()
        ref = artifacts.put(ModelSelection(model_id="gpt-5.6-sol").model_dump_json().encode(), "model")
        state = state.replace(stable_snapshot=state.stable_snapshot.replace(agent_plugin_id="codex_cli_agent",
            config={"allowed_models": list(allowed)}, artifacts={**state.stable_snapshot.artifacts, "model": ref}),
            feasible_targets=["prompt", "model"])
        return artifacts, EvolutionRequest(attempt_id="model-attempt", round_id=1, context=state,
            decision=EvolutionDecision(target="model", rationale="public evidence"),
            operator_id="model_selection_evolver", max_candidates=1, run_context=live)

    def test_each_supported_model_is_selectable_without_changing_the_parent_prompt(self):
        for selected in SUPPORTED_MODELS[:-1]:
            with self.subTest(selected=selected):
                artifacts, request = self.request()
                stable = request.context.stable_snapshot
                model = RecordingModel(lambda *_: response(json.dumps({"model_id": selected, "rationale": "try an alternative"})))
                result = ModelSelectionEvolver(ModelSelectionEvolverConfig(max_candidates=1), artifacts, model).evolve(request)
                self.assertEqual(result.status, "completed")
                self.assertEqual(len(result.candidates), 1)
                candidate = result.candidates[0]
                self.assertEqual(set(candidate.artifact_updates), {"model"})
                self.assertEqual(ModelSelection.model_validate_json(artifacts.get(candidate.artifact_updates["model"])).model_id, selected)
                self.assertEqual(request.context.stable_snapshot, stable)
                self.assertEqual(result.usage.model_calls, 1)
                payload = json.loads(model.requests[0][0].messages[1].content)
                self.assertNotIn("gpt-5.6-sol", payload["allowed_models"])
                self.assertNotIn("private_parameters", json.dumps(payload))

    def test_invalid_choices_fail_and_no_change_is_empty(self):
        for chosen, status in (("not-allowed", "error"), ("gpt-5.6-sol", "empty"), (None, "empty")):
            with self.subTest(chosen=chosen):
                artifacts, request = self.request()
                model = RecordingModel(lambda *_: response(json.dumps({"model_id": chosen, "rationale": "test"})))
                result = ModelSelectionEvolver(ModelSelectionEvolverConfig(max_candidates=1), artifacts, model).evolve(request)
                self.assertEqual(result.status, status)
                self.assertFalse(result.candidates)
                self.assertEqual(result.usage.model_calls, 1)

    def test_exhausted_catalog_makes_the_operator_inapplicable_without_a_model_call(self):
        artifacts, request = self.request(["gpt-5.6-sol"])
        model = RecordingModel(lambda *_: self.fail("no model call should occur"))
        operator = ModelSelectionEvolver(ModelSelectionEvolverConfig(max_candidates=1), artifacts, model)
        self.assertFalse(operator.is_applicable(request.context.stable_snapshot))
        self.assertEqual(operator.evolve(request).status, "empty")

    def test_selection_can_return_to_the_initial_model_from_another_stable_model(self):
        artifacts, request = self.request()
        previous = artifacts.put(ModelSelection(model_id="gpt-6-sol").model_dump_json().encode(), "model")
        stable = request.context.stable_snapshot.replace(artifacts={**request.context.stable_snapshot.artifacts, "model": previous})
        request = EvolutionRequest(attempt_id=request.attempt_id, round_id=request.round_id,
            context=request.context.replace(stable_snapshot=stable), decision=request.decision,
            operator_id=request.operator_id, max_candidates=1, run_context=request.run_context)
        model = RecordingModel(lambda *_: response('{"model_id":"gpt-5.6-sol","rationale":"validate a return"}'))
        result = ModelSelectionEvolver(ModelSelectionEvolverConfig(max_candidates=1), artifacts, model).evolve(request)
        self.assertEqual(result.status, "completed")
        self.assertEqual(ModelSelection.model_validate_json(artifacts.get(result.candidates[0].artifact_updates["model"])).model_id, "gpt-5.6-sol")

