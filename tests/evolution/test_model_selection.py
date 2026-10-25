import json
import unittest

from aer_core.schemas.evolution import EvolutionDecision, EvolutionRequest
from plugins.agents.model_selection import CodexModelAdapter, ModelSelection, SUPPORTED_MODELS
from plugins.evolution.model_selection import ModelSelectionEvolver, ModelSelectionEvolverConfig
from tests.evolution.helpers import evolution_context
from tests.execution.helpers import RecordingModel, response


class ModelSelectionTests(unittest.TestCase):
    def request(self, allowed=SUPPORTED_MODELS, *, efforts=None, current_effort=None):
        state, artifacts, live = evolution_context()
        ref = artifacts.put(ModelSelection(model_id="gpt-5.6-sol", reasoning_effort=current_effort).model_dump_json().encode(), "model")
        state = state.replace(stable_snapshot=state.stable_snapshot.replace(agent_plugin_id="codex_cli_agent",
            config={"allowed_models": list(allowed), "allowed_reasoning_efforts": efforts or {}},
            artifacts={**state.stable_snapshot.artifacts, "model": ref}),
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
                result = ModelSelectionEvolver(ModelSelectionEvolverConfig(max_candidates=1), artifacts, model, adapter=CodexModelAdapter()).evolve(request)
                self.assertEqual(result.status, "completed")
                self.assertEqual(len(result.candidates), 1)
                candidate = result.candidates[0]
                self.assertEqual(set(candidate.artifact_updates), {"model"})
                self.assertEqual(ModelSelection.model_validate_json(artifacts.get(candidate.artifact_updates["model"])).model_id, selected)
                self.assertEqual(request.context.stable_snapshot, stable)
                self.assertEqual(result.usage.model_calls, 1)
                payload = json.loads(model.requests[0][0].messages[1].content)
                self.assertEqual(payload["routing_decision"]["rationale"], "public evidence")
                self.assertNotIn("gpt-5.6-sol", payload["allowed_models"])
                self.assertNotIn("private_parameters", json.dumps(payload))
                self.assertEqual(candidate.metadata["change_summary"], f"Execution model: gpt-5.6-sol -> {selected}")

    def test_invalid_choices_fail_and_no_change_is_empty(self):
        for chosen, status in (("not-allowed", "error"), ("gpt-5.6-sol", "empty"), (None, "empty")):
            with self.subTest(chosen=chosen):
                artifacts, request = self.request()
                model = RecordingModel(lambda *_: response(json.dumps({"model_id": chosen, "rationale": "test"})))
                result = ModelSelectionEvolver(ModelSelectionEvolverConfig(max_candidates=1), artifacts, model, adapter=CodexModelAdapter()).evolve(request)
                self.assertEqual(result.status, status)
                self.assertFalse(result.candidates)
                self.assertEqual(result.usage.model_calls, 1)

    def test_exhausted_catalog_makes_the_operator_inapplicable_without_a_model_call(self):
        artifacts, request = self.request(["gpt-5.6-sol"])
        model = RecordingModel(lambda *_: self.fail("no model call should occur"))
        operator = ModelSelectionEvolver(ModelSelectionEvolverConfig(max_candidates=1), artifacts, model, adapter=CodexModelAdapter())
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
        result = ModelSelectionEvolver(ModelSelectionEvolverConfig(max_candidates=1), artifacts, model, adapter=CodexModelAdapter()).evolve(request)
        self.assertEqual(result.status, "completed")
        self.assertEqual(ModelSelection.model_validate_json(artifacts.get(result.candidates[0].artifact_updates["model"])).model_id, "gpt-5.6-sol")

    def test_effort_only_model_only_and_joint_changes_are_selected_as_complete_configurations(self):
        efforts = {name: ["medium", "high"] for name in SUPPORTED_MODELS}
        for name, effort in (("gpt-5.6-sol", "high"), ("gpt-6-sol", "medium"), ("gpt-6-sol", "high")):
            with self.subTest(model=name, effort=effort):
                artifacts, request = self.request(efforts=efforts, current_effort="medium")
                proposer = RecordingModel(lambda *_: response(json.dumps({"model_id": name,
                    "reasoning_effort": effort, "rationale": "Compare the complete configuration"})))
                result = ModelSelectionEvolver(ModelSelectionEvolverConfig(max_candidates=1), artifacts, proposer,
                    adapter=CodexModelAdapter()).evolve(request)
                self.assertEqual(result.status, "completed", result.error)
                selected = ModelSelection.model_validate_json(artifacts.get(result.candidates[0].artifact_updates["model"]))
                self.assertEqual(selected, ModelSelection(model_id=name, reasoning_effort=effort))
                payload = json.loads(proposer.requests[0][0].messages[-1].content)
                self.assertEqual(payload["current_selection"]["reasoning_effort"], "medium")
                self.assertIn(selected.model_dump(mode="json"), payload["allowed_selections"])
                self.assertEqual(set(result.candidates[0].artifact_updates), {"model"})

    def test_one_model_with_multiple_efforts_is_applicable_and_invalid_pairs_are_rejected(self):
        artifacts, request = self.request(["gpt-5.6-sol"], efforts={"gpt-5.6-sol": ["medium", "high"]}, current_effort="medium")
        for choice, status in (({"model_id": "gpt-5.6-sol", "reasoning_effort": "medium"}, "empty"),
                               ({"model_id": "gpt-5.6-sol", "reasoning_effort": "low"}, "error"),
                               ({"model_id": "gpt-6-sol", "reasoning_effort": "high"}, "error"),
                               ({"model_id": None, "reasoning_effort": "high"}, "error")):
            with self.subTest(choice=choice):
                model = RecordingModel(lambda *_: response(json.dumps({**choice, "rationale": "test"})))
                operator = ModelSelectionEvolver(ModelSelectionEvolverConfig(max_candidates=1), artifacts, model, adapter=CodexModelAdapter())
                self.assertTrue(operator.is_applicable(request.context.stable_snapshot))
                result = operator.evolve(request)
                self.assertEqual(result.status, status)
                self.assertFalse(result.candidates)

    def test_default_effort_is_distinct_from_no_proposal_and_legacy_response_preserves_effort(self):
        artifacts, request = self.request(efforts={name: [None, "high"] for name in SUPPORTED_MODELS}, current_effort="high")
        for value, expected, status in (({"model_id": "gpt-6-sol"}, "high", "completed"),
            ({"model_id": "gpt-5.6-sol", "reasoning_effort": None}, None, "completed"),
            ({"model_id": None, "reasoning_effort": None}, None, "empty")):
            with self.subTest(value=value):
                model = RecordingModel(lambda *_: response(json.dumps({**value, "rationale": "test"})))
                result = ModelSelectionEvolver(ModelSelectionEvolverConfig(max_candidates=1), artifacts, model,
                    adapter=CodexModelAdapter()).evolve(request)
                self.assertEqual(result.status, status, result.error)
                if result.candidates:
                    selected = ModelSelection.model_validate_json(artifacts.get(result.candidates[0].artifact_updates["model"]))
                    self.assertEqual(selected.reasoning_effort, expected)

    def test_same_evolver_can_use_an_adapter_for_a_different_execution_agent(self):
        class OtherAdapter:
            compatible_agents = ("other_agent",)

            def options(self, snapshot):
                return (ModelSelection(model_id="vendor-model-a"), ModelSelection(model_id="vendor-model-b"))

            def read(self, snapshot, artifacts):
                return ModelSelection.model_validate_json(artifacts.get(snapshot.artifacts["model"]))

            def encode(self, selection):
                return selection.model_dump_json(exclude_none=True).encode()

        artifacts, request = self.request()
        current = artifacts.put(b'{"model_id":"vendor-model-a"}', "model")
        stable = request.context.stable_snapshot.replace(agent_plugin_id="other_agent", config={},
            artifacts={**request.context.stable_snapshot.artifacts, "model": current})
        request = EvolutionRequest(attempt_id=request.attempt_id, round_id=request.round_id,
            context=request.context.replace(stable_snapshot=stable), decision=request.decision,
            operator_id=request.operator_id, max_candidates=1, run_context=request.run_context)
        model = RecordingModel(lambda *_: response('{"model_id":"vendor-model-b","reasoning_effort":null,"rationale":"test"}'))
        operator = ModelSelectionEvolver(ModelSelectionEvolverConfig(max_candidates=1), artifacts, model, adapter=OtherAdapter())
        result = operator.evolve(request)
        self.assertEqual(result.status, "completed", result.error)
        self.assertEqual(operator.capabilities().compatible_agents, ("other_agent",))
        self.assertIn("vendor-model-b", artifacts.get(result.candidates[0].artifact_updates["model"]).decode())

