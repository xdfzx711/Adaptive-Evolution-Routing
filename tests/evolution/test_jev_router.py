import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from pydantic import SecretStr

from aer_core.errors import PluginConfigurationError, RoutingError
from aer_core.schemas.evolution import CandidateSummary, EvolutionDecision, ExperienceSummary
from aer_core.schemas.history import ComponentEdit, ScoreChange, ValidationSummary
from aer_core.schemas.routing import JevRoutingRecord
from experiments.routing_report import routing_record, routing_view
from plugins.routers.jev import (JevCredentials, JevRouter, JevRouterOptions,
                                 JevRoutingSettings, resolve_jev_credentials)
from tests.evolution.helpers import evolution_context


class JevRouterTests(unittest.TestCase):
    def test_skill_competition_receives_operator_boundaries_and_prior_skip_reason(self):
        from aer_core.schemas.construction import ConstructionFeedback, ConstructionIssue
        from plugins.routers.skill_guidance import SKILL_ROUTING_GUIDANCE
        state, artifacts = self.two_targets()
        previous = ExperienceSummary(attempt_id="skip-previous", run_id="run-1", episode_id="episode-1",
            task_id=state.task.task_id, round_id=1, stable_version_id=state.stable_snapshot.version_id,
            decision=EvolutionDecision(target="skill", rationale="Investigate the skill"), diagnosis=None,
            candidate_summaries=[], completed_at=state.as_of, attempt_status="empty_candidates",
            construction_feedback=ConstructionFeedback(component="skill", operator_id="skillclaw_skill_evolver",
                outcome="empty", issues=[ConstructionIssue(stage="judgment", code="no_skill_change", source="model_assessment",
                    message="Existing skill is adequate; Agent ignored its guidance.")]))
        state = state.replace(round_id=2, history=[previous], feasible_targets=["prompt", "skill"],
            target_descriptions={"prompt": "Change instructions", "skill": "Create or improve skills, or skip"})
        bodies = []
        def respond(request):
            bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"answers": {"evolution_target": {
                "type": "choice", "choice": "prompt", "confidence": 0.8,
                "probabilities": {"prompt": 0.8, "skill": 0.2}}},
                "usage": {"input_tokens": 20, "output_tokens": 10}})
        router = JevRouter(self.config(), artifacts, self.credentials(), transport=httpx.MockTransport(respond))
        self.assertEqual(router.select(state).target, "prompt")
        self.assertTrue(bodies[0]["questions"]["evolution_target"]["instructions"].endswith(SKILL_ROUTING_GUIDANCE))
        self.assertEqual(bodies[0]["questions"]["evolution_target"]["criteria"], dict(state.target_descriptions))
        self.assertIn("Agent ignored", json.dumps(bodies[0]["state"]["history"][0]["construction_feedback"]))

    def config(self, **options):
        return JevRoutingSettings(plugin="jev_router", fixed_target=None,
            manual_decisions=[], config=JevRouterOptions(**options))

    def credentials(self):
        return JevCredentials(SecretStr("private-test-key"), "https://jev.example", "jev-test")

    def test_base_url_accepts_api_root_v1_and_full_endpoint(self):
        urls = ("https://jev.example", "https://jev.example/v1", "https://jev.example/v1/systemone")
        for url in urls:
            with self.subTest(url=url):
                credentials = JevCredentials(SecretStr("key"), url, "jev-test")
                self.assertEqual(credentials.endpoint, "https://jev.example/v1/systemone")

    def two_targets(self):
        state, artifacts, _ = evolution_context()
        state = state.replace(feasible_targets=["prompt", "model"],
            target_descriptions={"prompt": "Change reusable instructions", "model": "Select another execution model"})
        return state, artifacts

    def test_choice_request_and_typed_receipt_exclude_credentials_and_private_data(self):
        state, artifacts = self.two_targets()
        requests = []
        def respond(request):
            requests.append(request)
            return httpx.Response(200, json={"model": "jev-1.13.0", "answers": {
                "evolution_target": {"type": "choice", "choice": "model", "confidence": 0.65,
                    "probabilities": {"prompt": 0.2, "model": 0.8}}},
                "usage": {"input_tokens": 120, "output_tokens": 10}})
        router = JevRouter(self.config(), artifacts, self.credentials(), transport=httpx.MockTransport(respond))
        decision = router.select(state)
        self.assertEqual(decision.target, "model")
        self.assertIn("0.800", decision.rationale)
        self.assertEqual((decision.usage.total_tokens, decision.usage.model_calls), (130, 1))
        self.assertEqual(requests[0].url.path, "/v1/systemone")
        self.assertEqual(requests[0].headers["Authorization"], "Bearer private-test-key")
        body = json.loads(requests[0].content)
        self.assertEqual(body["model"], "jev-test")
        self.assertEqual(set(body["questions"]["evolution_target"]["criteria"]), {"prompt", "model"})
        self.assertNotIn("run_context", json.dumps(body))
        self.assertNotIn("eval_spec_ref", json.dumps(body))
        saved = JevRoutingRecord.model_validate_json(artifacts.get(decision.record_ref))
        self.assertEqual(saved.model_dump(mode="json")["request"], body)
        self.assertEqual(saved.response["answers"]["evolution_target"]["choice"], "model")
        self.assertNotIn("private-test-key", saved.model_dump_json())
        view = routing_view(routing_record(SimpleNamespace(decision=decision, response=None), artifacts))
        self.assertEqual(view["request"], body)
        self.assertEqual(view["response"], saved.model_dump(mode="json")["response"])

    def test_one_feasible_target_skips_jev_call_and_records_why(self):
        state, artifacts, _ = evolution_context()
        def no_call(request):
            self.fail("Jev should not be called for one feasible target")
        router = JevRouter(self.config(), artifacts, self.credentials(), transport=httpx.MockTransport(no_call))
        decision = router.select(state)
        saved = JevRoutingRecord.model_validate_json(artifacts.get(decision.record_ref))
        self.assertEqual(decision.target, "prompt")
        self.assertEqual(saved.source, "single_feasible_target")
        self.assertEqual(saved.usage.model_calls, 0)
        self.assertIsNone(saved.response)

    def test_history_preserves_actual_edits_scores_and_version_outcome_in_jev_request(self):
        state, artifacts = self.two_targets()
        instruction = "Inspect all caller paths before choosing an exception boundary. " * 25
        scores = ValidationSummary(status="complete", current_task=ScoreChange(before=0, after=0, delta=0),
            independent=ScoreChange(before=0.5, after=0.5, delta=0))
        candidate = CandidateSummary(candidate_id="candidate-previous", target="prompt", updated_components=["prompt"],
            actual_changes=[ComponentEdit(component="prompt", operation="insert", before="", after=instruction)],
            validation=scores, reward=0, delta_score=0, delta_similar_score=0, delta_regression_score=0,
            accepted=False, selected=False, committed=False, reason="rejected", acceptance_reason="no_improvement")
        previous = ExperienceSummary(attempt_id="previous", run_id="run-1", episode_id="episode-1", task_id=state.task.task_id,
            round_id=1, stable_version_id=state.stable_snapshot.version_id, stable_version_after=state.stable_snapshot.version_id,
            decision=EvolutionDecision(target="prompt", rationale="Use the public failure evidence"), diagnosis=None,
            candidate_summaries=[candidate], completed_at=state.as_of, attempt_status="completed")
        state = state.replace(round_id=2, history=[previous])
        router = JevRouter(self.config(max_field_chars=40), artifacts, self.credentials(), transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"answers": {"evolution_target": {
                "type": "choice", "choice": "model", "confidence": 0.5,
                "probabilities": {"prompt": 0.3, "model": 0.7}}}, "usage": {"input_tokens": 3, "output_tokens": 2}})))
        decision = router.select(state)
        saved = JevRoutingRecord.model_validate_json(artifacts.get(decision.record_ref))
        history = saved.request["state"]["history"][0]
        self.assertEqual(history["stable_version_after"], state.stable_snapshot.version_id)
        self.assertEqual(history["candidates"][0]["actual_changes"][0]["after"], instruction)
        self.assertEqual(history["candidates"][0]["validation"]["independent"]["before"], 0.5)

    def test_invalid_choice_keeps_call_record_and_actual_usage(self):
        state, artifacts = self.two_targets()
        router = JevRouter(self.config(), artifacts, self.credentials(), transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"answers": {"evolution_target": {
                "type": "choice", "choice": "memory", "confidence": 0.8,
                "probabilities": {"prompt": 0.2, "model": 0.8}}},
                "usage": {"input_tokens": 3, "output_tokens": 2}})))
        with self.assertRaises(RoutingError) as caught:
            router.select(state)
        info = caught.exception.to_info()
        saved = JevRoutingRecord.model_validate_json(artifacts.get(info.evidence_ref))
        self.assertEqual(info.usage.total_tokens, 5)
        self.assertIsNone(saved.decision)
        self.assertIsNotNone(saved.error)
        view = routing_view(routing_record(SimpleNamespace(decision=None,
            response=SimpleNamespace(error=info)), artifacts))
        self.assertEqual(view["response"]["answers"]["evolution_target"]["choice"], "memory")
        self.assertIsNotNone(view["error"])

    def test_local_state_limit_fails_before_api_without_charging_a_call(self):
        state, artifacts = self.two_targets()
        router = JevRouter(self.config(max_state_chars=20), artifacts, self.credentials(), transport=httpx.MockTransport(
            lambda request: self.fail("oversize state must not reach Jev")))
        with self.assertRaises(RoutingError) as caught:
            router.select(state)
        saved = JevRoutingRecord.model_validate_json(artifacts.get(caught.exception.to_info().evidence_ref))
        self.assertEqual(saved.usage.model_calls, 0)

    def test_credentials_resolve_from_dotenv_without_adding_secrets_to_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("TEST_JEV_KEY=private-test-key\nTEST_JEV_URL=https://jev.example\nTEST_JEV_MODEL=jev-test\n", encoding="utf-8")
            config = JevRouterOptions(dotenv_path=".env", api_key_env="TEST_JEV_KEY",
                base_url_env="TEST_JEV_URL", model_env="TEST_JEV_MODEL")
            with patch.dict(os.environ, {}, clear=True):
                result = resolve_jev_credentials(config, Path(directory))
            self.assertEqual(result.model, "jev-test")
            self.assertNotIn("private-test-key", config.model_dump_json())
            path.write_text("TEST_JEV_MODEL=jev-test\n", encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True), self.assertRaises(PluginConfigurationError) as caught:
                resolve_jev_credentials(config, Path(directory))
            self.assertIn("TEST_JEV_KEY", str(caught.exception))
            self.assertNotIn("private-test-key", str(caught.exception))
