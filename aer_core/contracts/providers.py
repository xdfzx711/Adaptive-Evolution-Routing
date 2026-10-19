from typing import Protocol, runtime_checkable

from aer_core.schemas.providers import ModelRequest, ModelResponse, ToolCall, ToolResult, ToolSpec
from aer_core.schemas.runtime import RunContext


@runtime_checkable
class ModelProvider(Protocol):
    """Translate SDK-native data to AER envelopes and report usage on failures.

    Respect RunContext timeout/output allowance. Hidden reasoning text is not
    required. Unavailable capabilities raise UnsupportedCapability.
    """

    def generate(self, request: ModelRequest, run_ctx: RunContext) -> ModelResponse: ...


@runtime_checkable
class ToolProvider(Protocol):
    """Expose tool schemas and respect the configured call timeout.

    A recoverable tool failure may return ToolResult(status=error); an
    infrastructure failure raises PluginError. Both retain actual usage.
    RunContext.budget may be None while episode budget enforcement is deferred.
    """

    def specifications(self) -> tuple[ToolSpec, ...]: ...

    def invoke(self, call: ToolCall, run_ctx: RunContext) -> ToolResult: ...
