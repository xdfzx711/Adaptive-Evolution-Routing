import json
import unittest

import httpx
from pydantic import SecretStr

from aer_core.config.models import ModelProviderSettings
from aer_core.errors import ContractError, ExecutionError, OperationTimeout
from aer_core.schemas.providers import ModelMessage, ModelRequest
from plugins.benchmarks.toy_tasks import ToyBenchmark
from plugins.evaluators.deterministic import DeterministicEvaluator
from plugins.providers.qwen import QwenCredentials, QwenModel
from plugins.providers.tools import DeterministicTools
from tests.execution.helpers import TOOLS, agent_fixture, context


FAKE_KEY = "fake-qwen-key-for-tests"


def completion(content='{"answer":75}', *, calls=None, usage=None):
    return {"id": "chat-test", "model": "qwen-plus-test", "choices": [{"message": {
        "role": "assistant", "content": content, "tool_calls": calls,
        "reasoning_content": "hidden reasoning should not enter records"},
        "finish_reason": "tool_calls" if calls else "stop"}],
        "usage": usage if usage is not None else {"prompt_tokens": 100, "completion_tokens": 25,
            "total_tokens": 125, "prompt_tokens_details": {"cached_tokens": 40},
            "completion_tokens_details": {"reasoning_tokens": 10}}}


def settings(**config):
    return ModelProviderSettings(plugin="qwen_model", model_id="qwen-plus", config=config)


class QwenTests(unittest.TestCase):
    def request(self, tools=(), messages=None, seed=42):
        benchmark = ToyBenchmark()
        messages = messages or [ModelMessage(role="system", content="Return JSON."),
            ModelMessage(role="user", content=benchmark.task_view(benchmark.tasks("dev")[0]).model_dump_json())]
        return ModelRequest(messages=messages, tools=tools, seed=seed, max_output_tokens=2048)

    def provider(self, handler, **config):
        return QwenModel(settings(**config), QwenCredentials(SecretStr(FAKE_KEY), "https://qwen.example/v1"),
                         transport=httpx.MockTransport(handler))

    def test_wire_payload_and_native_timeout_without_credentials_in_records(self):
        def handler(request):
            self.assertEqual(str(request.url), "https://qwen.example/v1/chat/completions")
            self.assertEqual(request.headers["Authorization"], "Bearer " + FAKE_KEY)
            body = json.loads(request.content)
            self.assertEqual(body["model"], "qwen-plus")
            self.assertFalse(body["stream"])
            self.assertFalse(body["enable_thinking"])
            self.assertEqual(body["max_tokens"], 50)
            self.assertNotIn("seed", body)
            self.assertNotIn("extra_body", body)
            self.assertNotIn(FAKE_KEY, request.content.decode())
            self.assertEqual(request.extensions["timeout"]["read"], 30)
            return httpx.Response(200, json=completion())
        model = self.provider(handler)
        result = model.generate(self.request(), context())
        self.assertEqual(result.usage.input_tokens, 100)
        self.assertEqual(result.usage.output_tokens, 25)
        self.assertEqual(result.usage.usage_source, "provider_reported")
        self.assertEqual(result.metadata["completion_tokens_details"]["reasoning_tokens"], 10)
        self.assertNotIn("hidden reasoning", result.model_dump_json())
        self.assertNotIn(FAKE_KEY, result.model_dump_json())
        self.assertNotIn(FAKE_KEY, json.dumps(model.runtime_info()))

    def test_tool_schemas_and_assistant_observation_roundtrip(self):
        calls = [{"id": "call-calculator", "type": "function", "function": {
            "name": "calculator", "arguments": '{"expression":"(17+8)*3"}'}}]
        requests = []
        def handler(request):
            body = json.loads(request.content)
            requests.append(body)
            if len(requests) == 1:
                self.assertEqual(body["tools"][0]["type"], "function")
                self.assertEqual(body["tools"][0]["function"]["name"], "calculator")
                self.assertEqual(body["tool_choice"], "auto")
                return httpx.Response(200, json=completion(None, calls=calls))
            sent_call = body["messages"][2]["tool_calls"][0]
            self.assertEqual(sent_call["id"], calls[0]["id"])
            self.assertEqual(json.loads(sent_call["function"]["arguments"]), json.loads(calls[0]["function"]["arguments"]))
            self.assertEqual(body["messages"][3]["role"], "tool")
            self.assertEqual(body["messages"][3]["tool_call_id"], "call-calculator")
            self.assertEqual(json.loads(body["messages"][3]["content"])["output"]["value"], 75)
            return httpx.Response(200, json=completion())
        model = self.provider(handler)
        agent, snapshot, _ = agent_fixture(model=model)
        data = snapshot.model_dump(mode="json")["config"]
        data["model"] = settings().model_dump(mode="json")
        # A fixture Agent starts from the matching Qwen-controlled Snapshot.
        snapshot = snapshot.replace(config=data)
        from aer_core.config.models import AgentSettings
        from plugins.agents.react import ReActAgent
        agent = ReActAgent(AgentSettings(plugin="react_agent", initial_prompt="fixture", initial_snapshot_ref=None, max_steps=8),
            snapshot, agent.artifacts, model, DeterministicTools(TOOLS))
        benchmark = ToyBenchmark()
        task = benchmark.tasks("dev")[0]
        result = agent.run(benchmark.task_view(task), snapshot, context(max_output_tokens=2048))
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.output["answer"], 75)
        self.assertEqual(result.usage.model_calls, 2)
        self.assertEqual(result.usage.tool_calls, 1)
        self.assertEqual(result.usage.total_tokens, 250)
        self.assertTrue(DeterministicEvaluator().evaluate_record(task, result, benchmark.evaluation_spec(task), operation_id="score").success)
        self.assertNotIn(FAKE_KEY, result.model_dump_json())
        self.assertNotIn("hidden reasoning", result.model_dump_json())

    def test_optional_fields_can_be_omitted_for_other_qwen_endpoints(self):
        def handler(request):
            body = json.loads(request.content)
            self.assertNotIn("enable_thinking", body)
            self.assertEqual(body["seed"], 42)
            self.assertEqual(body["response_format"], {"type": "json_object"})
            self.assertNotIn("tools", body)
            return httpx.Response(200, json=completion())
        self.provider(handler, enable_thinking=None, send_seed=True, response_format="json_object").generate(self.request(), context())

    def test_seed_range_fails_before_http_dispatch(self):
        def handler(request):
            self.fail("HTTP should not be dispatched")
        with self.assertRaises(ContractError) as caught:
            self.provider(handler, send_seed=True).generate(self.request(seed=2**31), context())
        self.assertEqual(caught.exception.to_info().usage.model_calls, 0)

    def test_http_errors_do_not_leak_error_body_or_retry(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(401, json={"error": {"message": FAKE_KEY, "code": "unauthorized"}})
        with self.assertRaises(ExecutionError) as caught:
            self.provider(handler).generate(self.request(), context())
        self.assertEqual(len(requests), 1)
        error = caught.exception.to_info()
        self.assertIn("HTTP 401", error.message)
        self.assertNotIn(FAKE_KEY, error.model_dump_json())
        self.assertEqual(error.usage.model_calls, 1)
        self.assertIsNone(error.usage.total_tokens)

    def test_timeout_and_network_failures_preserve_unknown_usage(self):
        for failure_type, error_type in [(httpx.ReadTimeout, OperationTimeout), (httpx.ConnectError, ExecutionError)]:
            def handler(request):
                raise failure_type(FAKE_KEY, request=request)
            with self.subTest(failure=failure_type), self.assertRaises(error_type) as caught:
                self.provider(handler).generate(self.request(), context())
            self.assertNotIn(FAKE_KEY, caught.exception.to_info().model_dump_json())
            self.assertEqual(caught.exception.to_info().usage.model_calls, 1)
            self.assertFalse(caught.exception.to_info().usage.usage_complete)

    def test_missing_and_partial_usage_are_not_estimated(self):
        for reported, expected in [(None, (None, None)), ({"prompt_tokens": 12}, (12, None)),
                                   ({"prompt_tokens": True, "completion_tokens": -1}, (None, None))]:
            data = completion()
            data["usage"] = reported
            model = self.provider(lambda request: httpx.Response(200, json=data))
            result = model.generate(self.request(), context())
            self.assertEqual((result.usage.input_tokens, result.usage.output_tokens), expected)
            self.assertEqual(result.usage.usage_source, "unknown")

    def test_malformed_response_and_arguments_keep_actual_usage(self):
        cases = [completion(calls=[{"id": "a", "type": "function", "function": {"name": "calculator", "arguments": "{"}}]),
                 completion(calls=[{"id": "a", "type": "function", "function": {"name": "calculator", "arguments": "[]"}}]),
                 {"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 25}}]
        for data in cases:
            with self.subTest(data=data), self.assertRaises(ContractError) as caught:
                self.provider(lambda request: httpx.Response(200, json=data)).generate(self.request(), context())
            self.assertEqual(caught.exception.to_info().usage.total_tokens, 125)

    def test_non_json_response_is_contract_error(self):
        with self.assertRaises(ContractError):
            self.provider(lambda request: httpx.Response(200, text="not JSON")).generate(self.request(), context())

    def test_full_endpoint_and_credential_repr(self):
        credentials = QwenCredentials(SecretStr(FAKE_KEY), "https://qwen.example/v1/chat/completions/")
        self.assertEqual(credentials.endpoint, "https://qwen.example/v1/chat/completions")
        self.assertNotIn(FAKE_KEY, repr(credentials))
        for url in ["ftp://host/v1", "https://user:password@host/v1", "https://host/v1?api_key=secret", ""]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                QwenCredentials(SecretStr(FAKE_KEY), url)
