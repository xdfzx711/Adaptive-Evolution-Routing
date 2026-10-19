"""A deterministic engineering provider that uses public inputs, not labels."""

import json
import re
from math import ceil
from time import monotonic

from aer_core.config.models import ModelProviderSettings
from aer_core.errors import ExecutionError, OperationTimeout
from aer_core.schemas.providers import ModelRequest, ModelResponse, ToolCall
from aer_core.schemas.runtime import RunContext
from aer_core.schemas.usage import ResourceUsage, UsageReport
from plugins.catalog import ScriptedModelConfig

from .calculator import CalculatorTimeout, calculate


class ScriptedModel:
    def __init__(self, config: ModelProviderSettings):
        if config.plugin != "scripted_model" or config.model_id != "scripted-v0.1":
            raise ValueError("unsupported scripted model identity")
        ScriptedModelConfig.model_validate(config.config)

    def generate(self, request: ModelRequest, run_ctx: RunContext) -> ModelResponse:
        start = monotonic()
        deadline = start + run_ctx.model_timeout_seconds
        input_tokens = ceil(len(json.dumps(request.model_dump(mode="json"), ensure_ascii=False)) / 4)
        try:
            user = next(message for message in request.messages if message.role == "user")
            task = json.loads(user.content)
            input_data = task.get("input", {}).get("data", {})
            tool_calls = ()
            output = {}
            if task.get("operation") == "failure_diagnosis":
                code = task["feedback"]["reason_code"]
                events = task["execution"]["trajectory"]
                limits = [event["seq"] for event in events if event["kind"] == "step_limit"]
                if limits:
                    code = "step_limit"
                steps = limits[-1:] or [event["seq"] for event in events[-1:]]
                output = {"reason_code": code, "explanation": "根据公开反馈与执行轨迹记录失败类型。",
                    "observations": [{"text": "公开执行记录与评分表明本次任务未通过。", "evidence_step_ids": steps}],
                    "hypotheses": [], "evidence_step_ids": steps, "confidence": None}
            elif task.get("operation") == "prompt_evolution":
                code = task["context"]["feedback"]["reason_code"]
                addition = {
                    "invalid_output_format": "Return only one JSON object with the declared fields and exact types. Do not use Markdown fences or explanation.",
                    "incorrect_answer": "Verify calculations and extracted values against the public input before giving the final answer.",
                }.get(code, "Use tool observations when available, then finish with the publicly requested JSON object.")
                output = {"candidates": [{"prompt": task["old_prompt"] + "\n" + addition,
                    "rationale": "Apply a reusable instruction based on the public failure category."}]}
            elif "expression" in input_data:
                observations = [message for message in request.messages if message.role == "tool"]
                if observations:
                    observation = json.loads(observations[-1].content)
                    if observation["status"] == "completed":
                        output = {"answer": observation["output"]["value"]}
                elif any(tool.name == "calculator" for tool in request.tools):
                    tool_calls = (ToolCall(call_id=f"{run_ctx.operation_id}/calculator", name="calculator",
                                          arguments={"expression": input_data["expression"]}),)
                else:
                    output = {"answer": calculate(input_data["expression"], deadline=deadline)}
            elif "text" in input_data:
                name = re.search(r"Name:\s*([A-Za-z]+)", input_data["text"])
                count = re.search(r"Count:\s*(\d+)", input_data["text"])
                if name and count:
                    output = {"name": name.group(1), "count": int(count.group(1))}
            elif "words" in input_data:
                output = {"answer": ",".join(word.upper() for word in sorted(input_data["words"]))}
            else:
                raise ValueError("scripted model does not support this public task input")
            content = None if tool_calls else json.dumps(output, ensure_ascii=False, allow_nan=False)
            generated = content if content is not None else json.dumps([call.model_dump(mode="json") for call in tool_calls])
            output_tokens = ceil(len(generated) / 4)
            finish_reason = "tool_calls" if tool_calls else "stop"
            allowance = min(request.max_output_tokens, run_ctx.max_output_tokens)
            if output_tokens > allowance:
                content, tool_calls = generated[:allowance * 4], ()
                output_tokens, finish_reason = allowance, "length"
            if monotonic() >= deadline:
                raise CalculatorTimeout("Model deadline reached")
            return ModelResponse(content=content, tool_calls=tool_calls, finish_reason=finish_reason,
                usage=UsageReport(input_tokens=input_tokens, output_tokens=output_tokens, model_calls=1, tool_calls=0,
                                  elapsed_seconds=monotonic() - start, usage_source="simulated"))
        except (ValueError, TypeError, KeyError, StopIteration, ArithmeticError, CalculatorTimeout) as failure:
            usage = ResourceUsage(input_tokens=input_tokens, output_tokens=0, total_tokens=input_tokens,
                model_calls=1, tool_calls=0, elapsed_seconds=monotonic() - start, usage_complete=True, usage_sources=["simulated"])
            error_type = OperationTimeout if isinstance(failure, CalculatorTimeout) else ExecutionError
            raise error_type(f"Scripted model could not produce a response: {failure}", stage=run_ctx.stage,
                operation_id=run_ctx.operation_id, plugin_id="scripted_model", usage=usage) from failure
