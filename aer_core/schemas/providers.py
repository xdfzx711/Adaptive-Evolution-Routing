"""SDK-neutral model and tool request/response envelopes."""

from typing import Literal

from pydantic import Field, model_validator

from .base import DomainModel, Identifier, Items, JsonObject, NonNegativeInt, PositiveInt
from .execution import ErrorInfo
from .usage import UsageReport


class ToolSpec(DomainModel):
    name: Identifier
    description: str
    parameters: JsonObject
    feedback_arguments: Items[Identifier] = ()
    feedback_outputs: Items[Identifier] = ()


class ToolCall(DomainModel):
    call_id: Identifier
    name: Identifier
    arguments: JsonObject


class ModelMessage(DomainModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str | None
    tool_calls: Items[ToolCall] = ()
    tool_call_id: Identifier | None = None

    @model_validator(mode="after")
    def tool_message_identity(self):
        if self.role == "tool" and self.tool_call_id is None:
            raise ValueError("tool messages require tool_call_id")
        if self.role != "tool" and self.tool_call_id is not None:
            raise ValueError("only tool messages may carry tool_call_id")
        if self.role != "assistant" and self.tool_calls:
            raise ValueError("only assistant messages may declare tool calls")
        return self


class ModelRequest(DomainModel):
    messages: Items[ModelMessage]
    tools: Items[ToolSpec] = ()
    seed: NonNegativeInt
    max_output_tokens: PositiveInt

    @model_validator(mode="after")
    def has_messages(self):
        if not self.messages:
            raise ValueError("model request requires messages")
        return self


class ModelResponse(DomainModel):
    content: str | None
    tool_calls: Items[ToolCall] = ()
    finish_reason: Identifier
    usage: UsageReport
    metadata: JsonObject = Field(default_factory=dict)

    @model_validator(mode="after")
    def has_response(self):
        if self.content is None and not self.tool_calls:
            raise ValueError("model response requires content or tool calls")
        ids = [call.call_id for call in self.tool_calls]
        if len(ids) != len(set(ids)):
            raise ValueError("tool call IDs must be unique")
        return self


class ToolResult(DomainModel):
    call_id: Identifier
    name: Identifier
    output: JsonObject
    status: Literal["completed", "error"]
    usage: UsageReport
    error: ErrorInfo | None = None

    @model_validator(mode="after")
    def tool_error(self):
        if (self.status == "error") != (self.error is not None):
            raise ValueError("tool error status must agree with ErrorInfo")
        return self
