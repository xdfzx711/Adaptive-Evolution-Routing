"""Stateless calculator calls with public schemas and measured local usage."""

from time import monotonic

from aer_core.config.models import ToolProviderSettings
from aer_core.errors import OperationTimeout
from aer_core.schemas.execution import ErrorInfo
from aer_core.schemas.providers import ToolCall, ToolResult, ToolSpec
from aer_core.schemas.runtime import RunContext
from aer_core.schemas.usage import ResourceUsage, UsageReport

from .calculator import CalculatorTimeout, calculate


class DeterministicTools:
    def __init__(self, config: ToolProviderSettings):
        if config.plugin != "deterministic_tools" or set(config.enabled) - {"calculator"}:
            raise ValueError("deterministic_tools only supports calculator")
        self.enabled = tuple(config.enabled)

    def specifications(self) -> tuple[ToolSpec, ...]:
        if "calculator" not in self.enabled:
            return ()
        return (ToolSpec(name="calculator", description="Evaluate arithmetic with +, -, *, / and parentheses.",
            feedback_arguments=["expression"], feedback_outputs=["value"],
            parameters={"type": "object", "properties": {"expression": {"type": "string"}},
                        "required": ["expression"], "additionalProperties": False}),)

    def invoke(self, call: ToolCall, run_ctx: RunContext) -> ToolResult:
        start = monotonic()
        deadline = start + run_ctx.tool_timeout_seconds
        error = None
        output = {}
        try:
            if call.name not in self.enabled:
                raise ValueError("requested tool is unavailable")
            if set(call.arguments) != {"expression"}:
                raise ValueError("calculator requires exactly the expression argument")
            output = {"value": calculate(call.arguments["expression"], deadline=deadline)}
        except CalculatorTimeout as failure:
            usage = ResourceUsage(input_tokens=0, output_tokens=0, total_tokens=0, model_calls=0,
                tool_calls=1, elapsed_seconds=monotonic() - start, usage_complete=True, usage_sources=["provider_reported"])
            raise OperationTimeout(str(failure), stage=run_ctx.stage, operation_id=run_ctx.operation_id,
                                   plugin_id="deterministic_tools", usage=usage) from failure
        except (ValueError, SyntaxError, ArithmeticError, RecursionError) as failure:
            error = ErrorInfo(code="tool_input_error", stage=run_ctx.stage, operation_id=run_ctx.operation_id,
                              plugin_id="deterministic_tools", message=str(failure))
        usage = UsageReport(input_tokens=0, output_tokens=0, model_calls=0, tool_calls=1,
                            elapsed_seconds=monotonic() - start, usage_source="provider_reported")
        return ToolResult(call_id=call.call_id, name=call.name, output=output,
                          status="error" if error else "completed", usage=usage, error=error)
