"""Synchronous Qwen Chat Completions adapter; credentials stay runtime-only."""

import json
from dataclasses import dataclass
from time import monotonic
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr

from aer_core.config.models import ModelProviderSettings
from aer_core.errors import ContractError, ExecutionError, OperationTimeout
from aer_core.schemas.providers import ModelRequest, ModelResponse, ToolCall
from aer_core.schemas.runtime import RunContext
from aer_core.schemas.usage import ResourceUsage, UsageReport
from plugins.catalog import QwenModelConfig


@dataclass(frozen=True)
class QwenCredentials:
    api_key: SecretStr
    base_url: str

    def __post_init__(self):
        if not self.api_key.get_secret_value().strip():
            raise ValueError("API key is empty")
        parts = urlsplit(self.base_url)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("base URL must be HTTP(S), without credentials, query or fragment")

    @property
    def endpoint(self) -> str:
        base = self.base_url.rstrip("/")
        return base if base.endswith("/chat/completions") else base + "/chat/completions"


def _json(text: str):
    def pairs(items):
        data = {}
        for key, value in items:
            if key in data:
                raise ValueError("duplicate JSON key")
            data[key] = value
        return data

    def invalid(value):
        raise ValueError("nonfinite JSON number")

    return json.loads(text, object_pairs_hook=pairs, parse_constant=invalid)


def _usage(data, elapsed: float) -> UsageReport:
    raw = data.get("usage") if isinstance(data, dict) else None
    raw = raw if isinstance(raw, dict) else {}

    def count(key):
        value = raw.get(key)
        return value if type(value) is int and value >= 0 else None

    input_tokens, output_tokens = count("prompt_tokens"), count("completion_tokens")
    return UsageReport(input_tokens=input_tokens, output_tokens=output_tokens, model_calls=1, tool_calls=0,
        elapsed_seconds=elapsed, usage_source="provider_reported" if input_tokens is not None and output_tokens is not None else "unknown")


def _resource(usage: UsageReport) -> ResourceUsage:
    known = usage.input_tokens is not None and usage.output_tokens is not None
    return ResourceUsage(input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
        total_tokens=usage.input_tokens + usage.output_tokens if known else None,
        model_calls=usage.model_calls, tool_calls=usage.tool_calls, elapsed_seconds=usage.elapsed_seconds,
        usage_complete=known and usage.usage_source != "unknown", usage_sources=[usage.usage_source])


class QwenModel:
    def __init__(self, settings: ModelProviderSettings, credentials: QwenCredentials,
                 *, transport: httpx.BaseTransport | None = None):
        if settings.plugin != "qwen_model":
            raise ValueError("Qwen provider settings must select qwen_model")
        if settings.model_id.startswith("${"):
            raise ValueError("Qwen model name must be resolved from the environment before provider creation")
        self.config = QwenModelConfig.model_validate(settings.config)
        self.model_id = settings.model_id
        self._credentials = credentials
        self._transport = transport

    def runtime_info(self) -> dict:
        return {"provider": "qwen_model", "model_id": self.model_id, "endpoint": self._credentials.endpoint,
            "protocol": "chat_completions", "stream": False, "send_seed": self.config.send_seed,
            "enable_thinking": self.config.enable_thinking, "temperature": self.config.temperature,
            "response_format": self.config.response_format, "automatic_retries": 0}

    def _payload(self, request: ModelRequest, run_ctx: RunContext) -> dict:
        messages = []
        for message in request.messages:
            item = {"role": message.role, "content": message.content}
            if message.tool_calls:
                item["tool_calls"] = [{"id": call.call_id, "type": "function", "function": {
                    "name": call.name, "arguments": json.dumps(call.model_dump(mode="json")["arguments"], ensure_ascii=False)}}
                    for call in message.tool_calls]
            if message.tool_call_id is not None:
                item["tool_call_id"] = message.tool_call_id
            messages.append(item)
        payload = {"model": self.model_id, "messages": messages, "stream": False,
            "max_tokens": min(request.max_output_tokens, run_ctx.max_output_tokens), "temperature": self.config.temperature}
        if request.tools:
            payload["tools"] = [{"type": "function", "function": tool.model_dump(mode="json",
                include={"name", "description", "parameters"})} for tool in request.tools]
            payload["tool_choice"] = "auto"
        if self.config.enable_thinking is not None:
            payload["enable_thinking"] = self.config.enable_thinking
        if self.config.send_seed:
            if request.seed > 2 ** 31 - 1:
                raise ContractError("Qwen seed must be at most 2^31-1", stage=run_ctx.stage,
                                    operation_id=run_ctx.operation_id, plugin_id="qwen_model", usage=ResourceUsage.zero())
            payload["seed"] = request.seed
        if self.config.response_format is not None:
            payload["response_format"] = {"type": self.config.response_format}
        return payload

    def generate(self, request: ModelRequest, run_ctx: RunContext) -> ModelResponse:
        payload = self._payload(request, run_ctx)
        start = monotonic()
        data = None
        try:
            with httpx.Client(transport=self._transport, timeout=run_ctx.model_timeout_seconds,
                              follow_redirects=False) as client:
                response = client.post(self._credentials.endpoint, json=payload,
                    headers={"Authorization": "Bearer " + self._credentials.api_key.get_secret_value()})
            try:
                data = _json(response.text)
            except ValueError:
                pass
            usage = _usage(data, monotonic() - start)
            if response.status_code >= 300:
                # Error bodies may echo request headers; keep them out of records.
                raise ExecutionError(f"Qwen request failed (HTTP {response.status_code})", stage=run_ctx.stage,
                    operation_id=run_ctx.operation_id, plugin_id="qwen_model", usage=_resource(usage))
            try:
                choices = data["choices"]
                if not isinstance(choices, list) or len(choices) != 1:
                    raise ValueError("expected one response choice")
                choice = choices[0]
                message = choice["message"]
                if message.get("role") != "assistant":
                    raise ValueError("response role must be assistant")
                calls = []
                for raw in message.get("tool_calls") or []:
                    if raw["type"] != "function":
                        raise ValueError("unsupported tool type")
                    arguments = _json(raw["function"]["arguments"])
                    if not isinstance(arguments, dict):
                        raise ValueError("tool arguments must be a JSON object")
                    calls.append(ToolCall(call_id=raw["id"], name=raw["function"]["name"], arguments=arguments))
                metadata = {key: data[key] for key in ("id", "model", "system_fingerprint") if isinstance(data.get(key), str)}
                raw_usage = data.get("usage")
                if isinstance(raw_usage, dict):
                    for key in ("prompt_tokens_details", "completion_tokens_details"):
                        details = raw_usage.get(key)
                        if isinstance(details, dict):
                            metadata[key] = {name: value for name, value in details.items() if type(value) is int and value >= 0}
                # reasoning_content is deliberately not part of ModelResponse.
                return ModelResponse(content=message.get("content"), tool_calls=calls,
                    finish_reason=choice["finish_reason"], usage=usage, metadata=metadata)
            except (ValueError, TypeError, KeyError, IndexError):
                raise ContractError("Qwen returned an invalid Chat Completions response", stage=run_ctx.stage,
                    operation_id=run_ctx.operation_id, plugin_id="qwen_model", usage=_resource(usage)) from None
        except httpx.TimeoutException:
            usage = _usage(data, monotonic() - start)
            raise OperationTimeout("Qwen HTTP request timed out", stage=run_ctx.stage,
                operation_id=run_ctx.operation_id, plugin_id="qwen_model", usage=_resource(usage)) from None
        except httpx.RequestError as failure:
            usage = _usage(data, monotonic() - start)
            raise ExecutionError(f"Qwen network request failed ({type(failure).__name__})", stage=run_ctx.stage,
                operation_id=run_ctx.operation_id, plugin_id="qwen_model", usage=_resource(usage)) from None
