import json
import unittest
from time import sleep

from aer_core.contracts.agent import AgentPlugin
from aer_core.errors import ExecutionError, UnsupportedCapability
from aer_core.schemas.execution import ExecutionResult
from aer_core.schemas.providers import ToolCall
from aer_core.schemas.runtime import RunContext, RunContextRecord
from aer_core.schemas.usage import ResourceUsage
from plugins.agents.react import parse_final
from plugins.benchmarks.toy_tasks import ToyBenchmark
from plugins.evaluators.deterministic import DeterministicEvaluator
from plugins.providers.scripted import ScriptedModel
from plugins.providers.tools import DeterministicTools
from tests.execution.helpers import MODEL, TOOLS, RecordingModel, agent_fixture, context, response


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.benchmark = ToyBenchmark()
        self.evaluator = DeterministicEvaluator()
        self.task = self.benchmark.task_view(self.benchmark.tasks("dev")[0])

    def test_both_agents_execute_all_splits_with_one_evaluator(self):
        for single in (False, True):
            agent, snapshot, _ = agent_fixture(single=single)
            self.assertIsInstance(agent, AgentPlugin)
            for split in ("dev", "validation", "test"):
                for task in self.benchmark.tasks(split):
                    with self.subTest(agent=agent.plugin_id, task=task.task_id):
                        result = agent.run(self.benchmark.task_view(task), snapshot, context(max_output_tokens=2048))
                        self.assertEqual(result.status, "completed")
                        score = self.evaluator.evaluate_record(task, result, self.benchmark.evaluation_spec(task), operation_id="score-test")
                        self.assertTrue(score.success)
                        self.assertEqual(result.agent_version_id, snapshot.version_id)
                        self.assertTrue(result.usage.usage_complete)
                        self.assertEqual(ExecutionResult.model_validate_json(result.model_dump_json()), result)
                        expected_tools = 0 if single or task.metadata["category"] != "arithmetic" else 1
                        self.assertEqual(result.usage.tool_calls, expected_tools)
                        self.assertEqual(result.usage.model_calls, 1 + expected_tools)

    def test_react_records_matching_tool_messages_and_unique_child_operations(self):
        scripted = ScriptedModel(MODEL)
        model = RecordingModel(scripted.generate)
        agent, snapshot, _ = agent_fixture(model=model)
        result = agent.run(self.task, snapshot, context(max_output_tokens=2048))
        self.assertEqual([event.kind for event in result.trajectory],
                         ["model_request", "model_response", "tool_call", "tool_result", "model_request", "model_response", "finish"])
        self.assertEqual([event.seq for event in result.trajectory], list(range(7)))
        request = model.requests[1][0]
        self.assertEqual([m.role for m in request.messages], ["system", "user", "assistant", "tool"])
        self.assertEqual(request.messages[2].tool_calls[0].call_id, request.messages[3].tool_call_id)
        self.assertEqual(len({ctx.operation_id for _, ctx in model.requests}), 2)
        self.assertNotIn("eval_spec_ref", request.messages[1].content)
        self.assertNotIn("expected", request.messages[1].content)

    def test_single_call_never_advertises_or_dispatches_tools(self):
        model = RecordingModel(lambda request, ctx: response('{"answer":75}'))
        agent, snapshot, _ = agent_fixture(single=True, model=model)
        result = agent.run(self.task, snapshot, context())
        self.assertEqual(len(model.requests), 1)
        self.assertEqual(model.requests[0][0].tools, ())
        self.assertEqual(result.usage.tool_calls, 0)
        self.assertEqual(result.usage.total_tokens, 5)

    def test_fresh_sessions_and_prompt_are_taken_from_passed_snapshot(self):
        model = RecordingModel(lambda request, ctx: response('{"answer":75}'))
        agent, snapshot, store = agent_fixture(model=model)
        candidate = snapshot.replace(version_id="candidate", parent_version_id=snapshot.version_id,
            artifacts={"prompt": store.put(b"A different candidate prompt.", "prompt").model_dump(mode="json")})
        first = agent.run(self.task, snapshot, context())
        second = agent.run(self.task, candidate, context())
        third = agent.run(self.task, snapshot, context())
        self.assertEqual([len(request.messages) for request, _ in model.requests], [2, 2, 2])
        self.assertEqual(model.requests[1][0].messages[0].content, "A different candidate prompt.")
        self.assertEqual(model.requests[0][0].messages[0].content, model.requests[2][0].messages[0].content)
        self.assertEqual(second.agent_version_id, "candidate")
        self.assertEqual(first.output, third.output)
        self.assertEqual(agent.snapshot(), snapshot)
        agent.restore(candidate)
        self.assertEqual(agent.snapshot(), candidate)
        self.assertEqual(agent.run(self.task, snapshot, context()).agent_version_id, snapshot.version_id)

    def test_step_limit_is_from_snapshot_and_includes_last_tool_cost(self):
        agent, snapshot, _ = agent_fixture()
        data = snapshot.model_dump(mode="json")["config"]
        data["max_steps"] = 1
        limited = snapshot.replace(version_id="limited", config=data)
        result = agent.run(self.task, limited, context(max_output_tokens=2048))
        self.assertEqual(result.status, "step_limit")
        self.assertEqual(result.output, {})
        self.assertEqual(result.usage.model_calls, 1)
        self.assertEqual(result.usage.tool_calls, 1)
        self.assertEqual(result.trajectory[-1].kind, "step_limit")
        self.assertEqual(result.state_summary["model_steps"], 1)

    def test_invalid_model_output_is_scoreable_format_failure(self):
        for content in ['answer: 75', '```json\n{"answer":75}\n```', '[75]', '{"answer":75,"answer":74}', '{"answer":NaN}']:
            model = RecordingModel(lambda request, ctx: response(content))
            agent, snapshot, _ = agent_fixture(model=model)
            with self.subTest(content=content):
                result = agent.run(self.task, snapshot, context())
                self.assertEqual(result.status, "completed")
                self.assertEqual(result.output, {})
                self.assertIn("output_parse_error", [e.kind for e in result.trajectory])
                task = self.benchmark.tasks("dev")[0]
                score = self.evaluator.evaluate_record(task, result, self.benchmark.evaluation_spec(task), operation_id="score")
                self.assertEqual(score.reason_code, "invalid_output_format")

    def test_tool_failure_can_be_observed_and_recovered(self):
        def generate(request, ctx):
            observations = [message for message in request.messages if message.role == "tool"]
            if not observations:
                return response(tool_calls=[ToolCall(call_id="bad-call", name="calculator", arguments={"expression": "1/0"})])
            self.assertEqual(json.loads(observations[-1].content)["status"], "error")
            return response('{"answer":75}')
        agent, snapshot, _ = agent_fixture(model=RecordingModel(generate))
        result = agent.run(self.task, snapshot, context())
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.output["answer"], 75)
        self.assertEqual(result.usage.model_calls, 2)
        self.assertEqual(result.usage.tool_calls, 1)
        self.assertEqual(next(e for e in result.trajectory if e.kind == "tool_result").payload["status"], "error")

    def test_multiple_tools_in_one_model_step_and_exact_usage_aggregation(self):
        def generate(request, ctx):
            if not any(message.role == "tool" for message in request.messages):
                return response(tool_calls=[ToolCall(call_id="a", name="calculator", arguments={"expression": "1+1"}),
                                            ToolCall(call_id="b", name="calculator", arguments={"expression": "2+2"})])
            return response('{"answer":75}')
        agent, snapshot, _ = agent_fixture(model=RecordingModel(generate))
        result = agent.run(self.task, snapshot, context())
        self.assertEqual(result.usage.model_calls, 2)
        self.assertEqual(result.usage.tool_calls, 2)
        self.assertEqual(result.usage.input_tokens, 6)
        self.assertEqual(result.usage.output_tokens, 4)
        self.assertEqual(result.state_summary["model_steps"], 2)
        self.assertEqual(result.usage.usage_sources, ("simulated", "provider_reported"))

    def test_provider_failure_preserves_known_usage_and_prior_calls(self):
        def generate(request, ctx):
            if not any(m.role == "tool" for m in request.messages):
                return response(tool_calls=[ToolCall(call_id="a", name="calculator", arguments={"expression": "1+1"})])
            usage = ResourceUsage(input_tokens=7, output_tokens=0, total_tokens=7, model_calls=1, tool_calls=0,
                                  elapsed_seconds=0.01, usage_complete=True, usage_sources=["provider_reported"])
            raise ExecutionError("provider failed", stage=ctx.stage, operation_id=ctx.operation_id, usage=usage)
        agent, snapshot, _ = agent_fixture(model=RecordingModel(generate))
        result = agent.run(self.task, snapshot, context())
        self.assertEqual(result.status, "error")
        self.assertEqual(result.usage.model_calls, 2)
        self.assertEqual(result.usage.tool_calls, 1)
        self.assertEqual(result.usage.total_tokens, 12)
        self.assertEqual(result.error.operation_id, result.operation_id)
        self.assertEqual(result.error.usage, result.usage)

    def test_unreported_failure_or_usage_does_not_fabricate_zero_tokens(self):
        def crash(request, ctx):
            raise RuntimeError("unreported failure")
        for generate in [crash, lambda request, ctx: response('{"answer":75}', unknown=True)]:
            agent, snapshot, _ = agent_fixture(model=RecordingModel(generate))
            result = agent.run(self.task, snapshot, context())
            self.assertFalse(result.usage.usage_complete)
            self.assertIsNone(result.usage.total_tokens)
            self.assertEqual(result.usage.model_calls, 1)
            self.assertIn("unknown", result.usage.usage_sources)

    def test_provider_contract_failures_retain_cost_without_double_counting(self):
        for single, generate in [(True, lambda request, ctx: response(tool_calls=[ToolCall(call_id="a", name="calculator", arguments={"expression": "1+1"})])),
                                 (False, lambda request, ctx: "wrong-response-type")]:
            agent, snapshot, _ = agent_fixture(single=single, model=RecordingModel(generate))
            result = agent.run(self.task, snapshot, context())
            self.assertEqual(result.status, "error")
            self.assertEqual(result.error.code, "contract_error")
            self.assertEqual(result.usage.model_calls, 1)
            self.assertEqual(result.usage.tool_calls, 0)

    def test_duplicate_tool_call_ids_across_steps_are_rejected(self):
        def generate(request, ctx):
            return response(tool_calls=[ToolCall(call_id="same", name="calculator", arguments={"expression": "1+1"})])
        agent, snapshot, _ = agent_fixture(model=RecordingModel(generate))
        result = agent.run(self.task, snapshot, context())
        self.assertEqual(result.status, "error")
        self.assertEqual(result.usage.model_calls, 2)
        self.assertEqual(result.usage.tool_calls, 1)

    def test_tool_infrastructure_failure_preserves_model_and_tool_cost(self):
        class BrokenTools(DeterministicTools):
            def invoke(self, call, run_ctx):
                usage = ResourceUsage(input_tokens=0, output_tokens=0, total_tokens=0, model_calls=0,
                    tool_calls=1, elapsed_seconds=0.01, usage_complete=True, usage_sources=["provider_reported"])
                raise ExecutionError("tool unavailable", stage=run_ctx.stage, operation_id=run_ctx.operation_id, usage=usage)
        model = RecordingModel(lambda request, ctx: response(tool_calls=[ToolCall(call_id="a", name="calculator", arguments={"expression": "1+1"})]))
        agent, snapshot, _ = agent_fixture(model=model, tools=BrokenTools(TOOLS))
        result = agent.run(self.task, snapshot, context())
        self.assertEqual(result.status, "error")
        self.assertEqual(result.usage.model_calls, 1)
        self.assertEqual(result.usage.tool_calls, 1)
        self.assertEqual(result.usage.total_tokens, 5)

    def test_mismatched_tool_result_identity_is_a_contract_failure(self):
        class MismatchedTools(DeterministicTools):
            def invoke(self, call, run_ctx):
                return super().invoke(call, run_ctx).replace(call_id="unrelated")
        model = RecordingModel(lambda request, ctx: response(tool_calls=[ToolCall(call_id="a", name="calculator", arguments={"expression": "1+1"})]))
        agent, snapshot, _ = agent_fixture(model=model, tools=MismatchedTools(TOOLS))
        result = agent.run(self.task, snapshot, context())
        self.assertEqual(result.status, "error")
        self.assertEqual(result.error.code, "contract_error")
        self.assertEqual(result.usage.tool_calls, 1)

    def test_timeout_after_completed_call_retains_actual_cost(self):
        def slow(request, ctx):
            sleep(0.01)
            return response('{"answer":75}')
        agent, snapshot, _ = agent_fixture(model=RecordingModel(slow))
        result = agent.run(self.task, snapshot, context(model_timeout_seconds=0.001))
        self.assertEqual(result.status, "error")
        self.assertEqual(result.error.code, "operation_timeout")
        self.assertEqual(result.usage.total_tokens, 5)

    def test_snapshot_provider_mismatch_and_foreign_agent_are_rejected(self):
        agent, snapshot, _ = agent_fixture()
        data = snapshot.model_dump(mode="json")["config"]
        data["model"]["model_id"] = "changed"
        for changed in [snapshot.replace(agent_plugin_id="single_call_agent"), snapshot.replace(config=data)]:
            with self.subTest(snapshot=changed), self.assertRaises(UnsupportedCapability):
                agent.run(self.task, changed, context())
            with self.assertRaises(UnsupportedCapability):
                agent.restore(changed)

    def test_budgetless_context_roundtrips_without_fake_remaining_allowance(self):
        live = context()
        record = live.to_record()
        self.assertIsNone(record.remaining_budget)
        saved = RunContextRecord.model_validate_json(record.model_dump_json())
        self.assertIsNone(RunContext.from_record(saved, budget=None).budget)

    def test_final_parser_requires_object_and_preserves_nested_values(self):
        self.assertEqual(parse_final('{"items":[1,2],"nested":{"ok":true}}'), {"items": [1, 2], "nested": {"ok": True}})
        for content in (None, "null", "42", '{"":1}'):
            with self.subTest(content=content), self.assertRaises(ValueError):
                parse_final(content)
