import json
import unittest

import httpx
from pydantic import SecretStr

from aer_core.config.models import RoutingSettings
from aer_core.errors import ExecutionError, RoutingError
from aer_core.schemas.routing import RoutingRecord
from plugins.providers.qwen import QwenCredentials, QwenModel
from plugins.routers.llm import LLMRouter, SYSTEM_PROMPT
from tests.evolution.helpers import evolution_context
from tests.execution.helpers import RecordingModel, response
from tests.execution.test_qwen import FAKE_KEY, completion, settings


CONFIG = RoutingSettings(plugin="llm_router", fixed_target=None, manual_decisions=[])


class LLMRouterTests(unittest.TestCase):
    def test_qwen_wire_input_and_saved_receipt_match_without_runtime_paths_or_labels(self):
        state, artifacts, _ = evolution_context()
        bodies = []
        def handler(request):
            bodies.append(json.loads(request.content))
            return httpx.Response(200, json=completion('{"target":"prompt","rationale":"输出要求需要完善"}'))
        model = QwenModel(settings(), QwenCredentials(SecretStr(FAKE_KEY), "https://qwen.example/v1"),
                          transport=httpx.MockTransport(handler))
        decision = LLMRouter(CONFIG, model, artifacts).select(state)
        saved = RoutingRecord.model_validate_json(artifacts.get(decision.record_ref))
        self.assertEqual(decision.target, "prompt")
        self.assertEqual(decision.usage.total_tokens, 125)
        self.assertEqual(saved.request.messages[0].content, SYSTEM_PROMPT)
        self.assertEqual([{"role": message.role, "content": message.content} for message in saved.request.messages], bodies[0]["messages"])
        public = json.loads(saved.request.messages[1].content)
        self.assertEqual(public["operation"], "evolution_routing")
        self.assertIn("prompt", public["stable_components"])
        for private in (FAKE_KEY, "workspace_ref", "eval_spec_ref", "private_parameters", "run_context"):
            self.assertNotIn(private, saved.request.model_dump_json())
        self.assertNotIn(FAKE_KEY, saved.model_dump_json())
        self.assertIsNone(saved.error)

    def test_invalid_decisions_and_tool_calls_keep_input_and_usage_without_fallback(self):
        invalid = [response("not JSON"), response('{"target":"memory","rationale":"unsupported"}'),
                   response('{"target":"prompt","rationale":"x","usage":{}}'),
                   response('{"target":"prompt","rationale":""}'),
                   response('{"target":"prompt","rationale":"x"}').replace(finish_reason="length")]
        for answer in invalid:
            with self.subTest(answer=answer.content):
                state, artifacts, _ = evolution_context()
                model = RecordingModel(lambda request, ctx: answer)
                with self.assertRaises(RoutingError) as failure:
                    LLMRouter(CONFIG, model, artifacts).select(state)
                info = failure.exception.to_info()
                self.assertEqual(info.stage, "routing")
                self.assertEqual(info.usage.model_calls, 1)
                saved = RoutingRecord.model_validate_json(artifacts.get(info.evidence_ref))
                self.assertEqual(saved.request, model.requests[0][0])
                self.assertIsNone(saved.decision)
                self.assertIsNotNone(saved.error)

    def test_provider_failure_retains_known_and_unknown_usage(self):
        for known in (True, False):
            state, artifacts, _ = evolution_context()
            def fail(request, ctx):
                if known:
                    from plugins.agents.execution import SessionRecord
                    record = SessionRecord()
                    record.add_usage(response("{}").usage)
                    raise ExecutionError("fixture failure", stage=ctx.stage, usage=record.usage())
                raise RuntimeError("fixture unknown failure")
            with self.assertRaises(RoutingError) as failure:
                LLMRouter(CONFIG, RecordingModel(fail), artifacts).select(state)
            saved = RoutingRecord.model_validate_json(artifacts.get(failure.exception.to_info().evidence_ref))
            self.assertEqual(saved.usage.model_calls, 1)
            self.assertEqual(saved.usage.usage_complete, known)
            self.assertIsNone(saved.response)


if __name__ == "__main__":
    unittest.main()
