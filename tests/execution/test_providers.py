import json
import unittest
from time import monotonic

from aer_core.contracts.providers import ModelProvider, ToolProvider
from aer_core.errors import ExecutionError, OperationTimeout
from aer_core.schemas.providers import ModelMessage, ModelRequest, ToolCall
from plugins.benchmarks.toy_tasks import ToyBenchmark
from plugins.providers.calculator import CalculatorTimeout, calculate
from plugins.providers.scripted import ScriptedModel
from plugins.providers.tools import DeterministicTools
from tests.execution.helpers import MODEL, TOOLS, context


class ProviderTests(unittest.TestCase):
    def test_calculator_arithmetic_and_integer_normalization(self):
        for expression, expected in [("(17 + 8) * 3", 75), ("84 / 7 - 5", 7), ("7 / 4", 1.75),
                                     ("-2 * (+3 + 4)", -14), ("2.5 + 0.5", 3)]:
            with self.subTest(expression=expression):
                value = calculate(expression)
                self.assertEqual(value, expected)
                self.assertEqual(type(value), type(expected))

    def test_calculator_rejects_unsupported_operations_and_invalid_numbers(self):
        for expression in ["2 ** 10", "abs(-2)", "x + 1", "True", "1e309", "1 / 0", "[]", "", "1" * 1025,
                           "+".join(["1"] * 80), "2" * 1024 + "*2"]:
            with self.subTest(expression=expression), self.assertRaises((ValueError, SyntaxError, ArithmeticError)):
                calculate(expression)

    def test_calculator_checks_deadline(self):
        with self.assertRaises(CalculatorTimeout):
            calculate("1 + 1", deadline=monotonic() - 1)

    def test_tools_report_calls_and_recoverable_errors(self):
        tools = DeterministicTools(TOOLS)
        self.assertIsInstance(tools, ToolProvider)
        self.assertEqual([tool.name for tool in tools.specifications()], ["calculator"])
        call = ToolCall(call_id="c1", name="calculator", arguments={"expression": "2 + 3"})
        good = tools.invoke(call, context())
        self.assertEqual(good.output["value"], 5)
        self.assertEqual(good.usage.tool_calls, 1)
        self.assertEqual(good.usage.input_tokens, 0)
        for changed in [call.replace(arguments={"expression": "1/0"}), call.replace(name="missing"),
                        call.replace(arguments={"expression": "2+3", "extra": True}),
                        call.replace(arguments={"expression": 2})]:
            with self.subTest(call=changed):
                result = tools.invoke(changed, context())
                self.assertEqual(result.status, "error")
                self.assertEqual(result.usage.tool_calls, 1)
                self.assertEqual(result.error.code, "tool_input_error")

    def test_disabled_tool_is_unavailable_and_timeouts_retain_usage(self):
        tools = DeterministicTools(TOOLS.model_copy(update={"enabled": []}))
        self.assertEqual(tools.specifications(), ())
        call = ToolCall(call_id="c1", name="calculator", arguments={"expression": "2+3"})
        self.assertEqual(tools.invoke(call, context()).status, "error")
        with self.assertRaises(OperationTimeout) as caught:
            DeterministicTools(TOOLS).invoke(call, context(tool_timeout_seconds=1e-12))
        self.assertEqual(caught.exception.to_info().usage.tool_calls, 1)

    def request(self, task=None, tools=(), messages=None, max_tokens=2048):
        benchmark = ToyBenchmark()
        task = task or benchmark.task_view(benchmark.tasks("dev")[0])
        messages = messages or [ModelMessage(role="system", content="Return the answer."),
                                ModelMessage(role="user", content=task.model_dump_json())]
        return ModelRequest(messages=messages, tools=tools, seed=42, max_output_tokens=max_tokens)

    def test_scripted_model_uses_public_input_and_tool_observation(self):
        model, tools = ScriptedModel(MODEL), DeterministicTools(TOOLS)
        self.assertIsInstance(model, ModelProvider)
        request = self.request(tools=tools.specifications())
        first = model.generate(request, context(max_output_tokens=2048))
        self.assertEqual(first.tool_calls[0].arguments["expression"], "(17 + 8) * 3")
        observation = tools.invoke(first.tool_calls[0], context())
        messages = list(request.messages) + [ModelMessage(role="assistant", content=None, tool_calls=first.tool_calls),
            ModelMessage(role="tool", content=observation.model_dump_json(), tool_call_id=observation.call_id)]
        final = model.generate(self.request(messages=messages), context())
        self.assertEqual(json.loads(final.content), {"answer": 75})
        self.assertEqual(final.usage.usage_source, "simulated")
        self.assertEqual(final.usage.model_calls, 1)

    def test_scripted_model_is_stateless_and_respects_output_allowance(self):
        model = ScriptedModel(MODEL)
        request = self.request()
        self.assertEqual(model.generate(request, context()).content, model.generate(request, context()).content)
        short = model.generate(self.request(max_tokens=1), context(max_output_tokens=1))
        self.assertEqual(short.finish_reason, "length")
        self.assertEqual(short.usage.output_tokens, 1)
        self.assertLessEqual(len(short.content), 4)

    def test_scripted_failures_and_timeouts_keep_simulated_call_cost(self):
        model = ScriptedModel(MODEL)
        task = ToyBenchmark().task_view(ToyBenchmark().tasks("dev")[0]).replace(input={"data": {"unknown": 1}})
        with self.assertRaises(ExecutionError) as caught:
            model.generate(self.request(task=task), context())
        self.assertEqual(caught.exception.to_info().usage.model_calls, 1)
        with self.assertRaises(OperationTimeout) as caught:
            model.generate(self.request(), context(model_timeout_seconds=1e-12))
        self.assertEqual(caught.exception.to_info().usage.usage_sources, ("simulated",))

    def test_scripted_model_does_not_depend_on_task_identity(self):
        benchmark = ToyBenchmark()
        task = benchmark.task_view(benchmark.tasks("dev")[0])
        model = ScriptedModel(MODEL)
        ordinary = model.generate(self.request(task=task), context())
        renamed = model.generate(self.request(task=task.replace(task_id="unseen-task")), context())
        self.assertEqual(ordinary.content, renamed.content)
        self.assertNotIn("private_parameters", self.request(task=task).model_dump_json())
