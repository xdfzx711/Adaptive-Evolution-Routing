import json
import unittest

from pydantic import ValidationError

from aer_core.errors import ExecutionError
from aer_core.schemas.evolution import EvolutionDecision, EvolutionRequest, EvolutionRequestRecord, EvolutionResponse
from plugins.evolution.prompt import PromptEvolverConfig, SimplePromptEvolver
from plugins.providers.scripted import ScriptedModel
from plugins.providers.qwen import QwenCredentials, QwenModel
from tests.execution.test_qwen import FAKE_KEY, completion, settings
from tests.evolution.helpers import evolution_context
from tests.execution.helpers import MODEL, RecordingModel, response


class PromptEvolverTests(unittest.TestCase):
    def request(self, state, live, max_candidates=1):
        return EvolutionRequest(attempt_id="attempt-test", round_id=state.round_id, context=state,
            decision=EvolutionDecision(target="prompt", rationale="fixed"), operator_id="simple_prompt_evolver",
            max_candidates=max_candidates, run_context=live)

    def test_scripted_evolver_supports_both_agents_and_preserves_old_artifact(self):
        for single in (False, True):
            state, artifacts, live = evolution_context(single)
            old_ref = state.stable_snapshot.artifacts["prompt"]
            old = artifacts.get(old_ref)
            evolver = SimplePromptEvolver(PromptEvolverConfig(max_candidates=1), artifacts, ScriptedModel(MODEL))
            result = evolver.evolve(self.request(state, live))
            self.assertEqual(result.status, "completed")
            self.assertEqual(len(result.candidates), 1)
            candidate = result.candidates[0]
            self.assertEqual(candidate.parent_version_id, state.stable_snapshot.version_id)
            self.assertNotEqual(artifacts.get(candidate.artifact_updates["prompt"]), old)
            self.assertEqual(artifacts.get(old_ref), old)
            self.assertEqual(result.usage.model_calls, 1)
            self.assertIn("simulated", result.usage.usage_sources)
            self.assertEqual(EvolutionResponse.model_validate_json(result.model_dump_json()), result)
            self.assertNotIn("version_store", vars(evolver))

    def test_model_receives_public_context_and_no_snapshot_paths_or_tools(self):
        state, artifacts, live = evolution_context()
        def generate(request, ctx):
            self.assertEqual(request.tools, ())
            payload = json.loads(request.messages[1].content)
            self.assertEqual(payload["operation"], "prompt_evolution")
            self.assertNotIn("stable_snapshot", payload["context"])
            self.assertNotIn("eval_spec_ref", request.model_dump_json())
            self.assertNotIn("private_parameters", request.model_dump_json())
            self.assertIn("old_prompt", payload)
            return response('{"candidates":[{"prompt":"Improved reusable instruction."}]}')
        result = SimplePromptEvolver(PromptEvolverConfig(max_candidates=1), artifacts, RecordingModel(generate)).evolve(self.request(state, live))
        self.assertEqual(result.status, "completed")

    def test_empty_and_identical_prompts_retain_generation_cost_without_new_artifacts(self):
        state, artifacts, live = evolution_context()
        old = artifacts.get(state.stable_snapshot.artifacts["prompt"]).decode()
        for payload in [{"candidates": []}, {"candidates": [{"prompt": old}]}]:
            before = len(artifacts._content)
            model = RecordingModel(lambda request, ctx: response(json.dumps(payload)))
            result = SimplePromptEvolver(PromptEvolverConfig(max_candidates=1), artifacts, model).evolve(self.request(state, live))
            self.assertEqual(result.status, "empty")
            self.assertEqual(result.usage.total_tokens, 5)
            self.assertEqual(len(artifacts._content), before)

    def test_multiple_candidates_and_duplicate_content(self):
        state, artifacts, live = evolution_context()
        payload = {"candidates": [{"prompt": "First instruction"}, {"prompt": "Second instruction"}, {"prompt": "First instruction"}]}
        model = RecordingModel(lambda request, ctx: response(json.dumps(payload)))
        result = SimplePromptEvolver(PromptEvolverConfig(max_candidates=3), artifacts, model).evolve(self.request(state, live, 3))
        self.assertEqual(result.status, "completed")
        self.assertEqual(len(result.candidates), 2)
        self.assertEqual(len({candidate.candidate_id for candidate in result.candidates}), 2)
        self.assertEqual(result.usage.model_calls, 1)

    def test_invalid_response_or_excess_candidates_are_errors_with_known_cost(self):
        state, artifacts, live = evolution_context()
        contents = ['not json', '{"candidates":[{"prompt":""}]}', '{"prompt":"wrong shape"}',
                    '{"candidates":[{"prompt":"one"},{"prompt":"two"}]}']
        for content in contents:
            before = len(artifacts._content)
            model = RecordingModel(lambda request, ctx: response(content))
            with self.subTest(content=content):
                result = SimplePromptEvolver(PromptEvolverConfig(max_candidates=1), artifacts, model).evolve(self.request(state, live))
                self.assertEqual(result.status, "error")
                self.assertEqual(result.candidates, ())
                self.assertEqual(result.usage.total_tokens, 5)
                self.assertEqual(len(artifacts._content), before)
                self.assertEqual(result.error.usage, result.usage)

    def test_unknown_provider_failure_preserves_incomplete_usage(self):
        state, artifacts, live = evolution_context()
        def crash(request, ctx):
            raise ExecutionError("unreported provider failure", stage=ctx.stage)
        result = SimplePromptEvolver(PromptEvolverConfig(max_candidates=1), artifacts,
                                     RecordingModel(crash)).evolve(self.request(state, live))
        self.assertEqual(result.status, "error")
        self.assertIsNone(result.usage.total_tokens)
        self.assertEqual(result.usage.model_calls, 1)
        self.assertFalse(result.usage.usage_complete)

    def test_round_identity_must_match_context(self):
        state, _, live = evolution_context(round_id=2)
        with self.assertRaises(ValidationError):
            EvolutionRequest(attempt_id="attempt", round_id=1, context=state,
                decision=EvolutionDecision(target="prompt", rationale="fixed"), operator_id="simple_prompt_evolver",
                max_candidates=1, run_context=live)

    def test_legacy_request_record_inherits_its_explicit_round(self):
        state, _, live = evolution_context(round_id=2)
        data = self.request(state, live).to_record().model_dump(mode="json")
        del data["context"]["round_id"]
        restored = EvolutionRequestRecord.model_validate(data)
        self.assertEqual(restored.context.round_id, 2)

    def test_qwen_prompt_generation_preserves_reported_usage_and_response_identity(self):
        import httpx
        from pydantic import SecretStr
        state, artifacts, live = evolution_context()
        def handler(request):
            body = json.loads(request.content)
            self.assertNotIn("tools", body)
            self.assertEqual(json.loads(body["messages"][1]["content"])["operation"], "prompt_evolution")
            return httpx.Response(200, json=completion('{"candidates":[{"prompt":"Use reusable verification instructions."}]}'))
        model = QwenModel(settings(), QwenCredentials(SecretStr(FAKE_KEY), "https://qwen.example/v1"),
                          transport=httpx.MockTransport(handler))
        result = SimplePromptEvolver(PromptEvolverConfig(max_candidates=1), artifacts, model).evolve(self.request(state, live))
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.usage.total_tokens, 125)
        self.assertEqual(result.usage.usage_sources, ("provider_reported",))
        self.assertEqual(result.candidates[0].metadata["model_metadata"]["id"], "chat-test")
        self.assertNotIn(FAKE_KEY, result.model_dump_json())
