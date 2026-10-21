"""ReAct-style model/tool interaction, with a fresh session on every run."""

import json
from dataclasses import replace
from time import monotonic

from pydantic import model_validator

from aer_core.config.models import AgentSettings, ModelProviderSettings, ToolProviderSettings
from aer_core.contracts.providers import ModelProvider, ToolProvider
from aer_core.contracts.storage import ArtifactStore
from aer_core.errors import AERError, BudgetExceeded, ContractError, ExecutionError, OperationTimeout, UnsupportedCapability
from aer_core.schemas.base import DomainModel, PositiveInt
from aer_core.schemas.evolution import AgentCapabilities
from aer_core.schemas.execution import ExecutionResult
from aer_core.schemas.providers import ModelMessage, ModelRequest, ModelResponse, ToolResult
from aer_core.schemas.references import AgentSnapshot
from aer_core.schemas.runtime import RunContext
from aer_core.schemas.task import TaskView

from .execution import SessionRecord
from plugins.json_output import parse_final


def snapshot_config(config):
    return {"max_steps": config.agent.max_steps, "model": config.providers.model.model_dump(mode="json"),
            "tools": config.providers.tools.model_dump(mode="json")}


def initial_artifacts(config):
    components = {"prompt": config.agent.initial_prompt.encode("utf-8")}
    return components


class ControlledConfig(DomainModel):
    max_steps: PositiveInt
    model: ModelProviderSettings
    tools: ToolProviderSettings


class LocalAgentSettings(AgentSettings):
    @model_validator(mode="after")
    def require_limit(self):
        if self.max_steps is None:
            raise ValueError("this Agent requires max_steps")
        if self.config:
            raise ValueError("this Agent has no plugin-specific config")
        return self


class ReActAgent:
    plugin_id = "react_agent"
    uses_tools = True

    def __init__(self, config: AgentSettings, initial_snapshot: AgentSnapshot,
                 artifact_store: ArtifactStore, model_provider: ModelProvider,
                 tool_provider: ToolProvider | None = None):
        if config.plugin != self.plugin_id:
            raise ValueError("Agent config does not match its factory")
        self.artifacts = artifact_store
        self.model = model_provider
        self.tools = tool_provider
        if self.uses_tools and self.tools is None:
            raise ValueError("ReAct Agent requires ToolProvider")
        self._snapshot = initial_snapshot
        controlled = self._controlled(initial_snapshot)
        self._provider_config = controlled.model.model_dump(mode="json")
        self._tool_config = controlled.tools.model_dump(mode="json")
        if controlled.max_steps != config.max_steps:
            raise ValueError("initial Snapshot max_steps differs from Agent configuration")
        self._validate_snapshot(initial_snapshot)

    def _controlled(self, snapshot: AgentSnapshot) -> ControlledConfig:
        if snapshot.agent_plugin_id != self.plugin_id:
            raise UnsupportedCapability("Snapshot belongs to another Agent", stage="setup", plugin_id=self.plugin_id)
        return ControlledConfig.model_validate(snapshot.model_dump(mode="json")["config"])

    def _validate_snapshot(self, snapshot: AgentSnapshot) -> ControlledConfig:
        controlled = self._controlled(snapshot)
        if controlled.model.model_dump(mode="json") != self._provider_config or controlled.tools.model_dump(mode="json") != self._tool_config:
            raise UnsupportedCapability("Snapshot model/tools differ from injected providers", stage="setup", plugin_id=self.plugin_id)
        if not self.uses_tools and controlled.max_steps != 1:
            raise UnsupportedCapability("single_call_agent requires max_steps=1", stage="setup", plugin_id=self.plugin_id)
        if "prompt" not in snapshot.artifacts or snapshot.artifacts["prompt"].kind != "prompt":
            raise UnsupportedCapability("Snapshot must contain a prompt artifact", stage="setup", plugin_id=self.plugin_id)
        if set(snapshot.artifacts) - set(self.capabilities().supported_artifact_kinds):
            raise UnsupportedCapability("Snapshot contains unsupported Agent components", stage="setup", plugin_id=self.plugin_id)
        return controlled

    def capabilities(self) -> AgentCapabilities:
        kinds = ["prompt"]
        return AgentCapabilities(supported_targets=kinds, supported_artifact_kinds=kinds,
            can_rebuild_session=True, snapshot_scope="controlled_config",
            feedback_state_fields=["agent_plugin", "model_steps", "effective_max_steps", "termination_reason", "fresh_session"])

    def snapshot(self) -> AgentSnapshot:
        return self._snapshot

    def restore(self, snapshot: AgentSnapshot) -> None:
        self._validate_snapshot(snapshot)
        prompt = self.artifacts.get(snapshot.artifacts["prompt"]).decode("utf-8")
        if not prompt.strip():
            raise UnsupportedCapability("Prompt cannot be blank", stage="setup", plugin_id=self.plugin_id)
        self._snapshot = snapshot

    def _failed_call(self, record: SessionRecord, error: Exception, *, model: bool):
        if isinstance(error, AERError) and error.to_info().usage is not None:
            record.add_usage(error.to_info().usage)
        else:
            record.unknown_call(model=model)

    def run(self, task: TaskView, snapshot: AgentSnapshot, run_ctx: RunContext) -> ExecutionResult:
        controlled = self._validate_snapshot(snapshot)
        max_steps = controlled.max_steps
        record = SessionRecord()
        output = {}
        steps = 0
        status = "completed"
        termination = "finish"
        error_info = None
        try:
            prompt = self.artifacts.get(snapshot.artifacts["prompt"]).decode("utf-8")
            if not prompt.strip():
                raise ValueError("Prompt cannot be blank")
            messages = [ModelMessage(role="system", content=prompt),
                        ModelMessage(role="user", content=task.model_dump_json())]
            tool_specs = self.tools.specifications() if self.uses_tools else ()
            if {tool.name for tool in tool_specs} != set(controlled.tools.enabled) and self.uses_tools:
                raise ContractError("Tool schemas do not match Snapshot enabled tools", stage=run_ctx.stage,
                                    operation_id=run_ctx.operation_id, plugin_id=self.plugin_id)
            seen_calls = set()
            for steps in range(1, max_steps + 1):
                model_ctx = replace(run_ctx, operation_id=f"{run_ctx.operation_id}/model/{steps}")
                request = ModelRequest(messages=messages, tools=tool_specs, seed=run_ctx.seed,
                                       max_output_tokens=run_ctx.max_output_tokens)
                record.event("model_request", {"operation_id": model_ctx.operation_id,
                    "request": request.model_dump(mode="json")})
                start = monotonic()
                try:
                    response = self.model.generate(request, model_ctx)
                except Exception as failure:
                    self._failed_call(record, failure, model=True)
                    raise
                if not isinstance(response, ModelResponse):
                    record.unknown_call(model=True)
                    raise ContractError("ModelProvider must return ModelResponse", stage=run_ctx.stage)
                record.add_usage(response.usage)
                record.event("model_response", {"operation_id": model_ctx.operation_id,
                                                **response.model_dump(mode="json")})
                if response.usage.model_calls < 1 or response.usage.tool_calls:
                    raise ContractError("ModelProvider usage must report model calls only", stage=run_ctx.stage)
                if monotonic() - start >= run_ctx.model_timeout_seconds:
                    raise OperationTimeout("ModelProvider exceeded configured timeout", stage=run_ctx.stage)
                if response.tool_calls:
                    if not self.uses_tools:
                        raise ContractError("Single-call Agent received an unsupported tool call", stage=run_ctx.stage)
                    messages.append(ModelMessage(role="assistant", content=response.content, tool_calls=response.tool_calls))
                    for call in response.tool_calls:
                        if call.call_id in seen_calls:
                            raise ContractError("Tool call IDs must be unique within a session", stage=run_ctx.stage)
                        seen_calls.add(call.call_id)
                        tool_ctx = replace(run_ctx, operation_id=f"{run_ctx.operation_id}/tool/{len(seen_calls)}")
                        record.event("tool_call", {"operation_id": tool_ctx.operation_id, **call.model_dump(mode="json")})
                        start = monotonic()
                        try:
                            observation = self.tools.invoke(call, tool_ctx)
                        except Exception as failure:
                            self._failed_call(record, failure, model=False)
                            raise
                        if not isinstance(observation, ToolResult):
                            record.unknown_call(model=False)
                            raise ContractError("ToolProvider must return ToolResult", stage=run_ctx.stage)
                        record.add_usage(observation.usage)
                        record.event("tool_result", {"operation_id": tool_ctx.operation_id,
                                                     **observation.model_dump(mode="json")})
                        if observation.call_id != call.call_id or observation.name != call.name:
                            raise ContractError("Tool result identity does not match request", stage=run_ctx.stage)
                        if observation.usage.tool_calls < 1 or observation.usage.model_calls:
                            raise ContractError("ToolProvider usage must report tool calls only", stage=run_ctx.stage)
                        if monotonic() - start >= run_ctx.tool_timeout_seconds:
                            raise OperationTimeout("ToolProvider exceeded configured timeout", stage=run_ctx.stage)
                        messages.append(ModelMessage(role="tool", content=observation.model_dump_json(), tool_call_id=call.call_id))
                    continue
                try:
                    output = parse_final(response.content)
                except ValueError as failure:
                    termination = "invalid_output_format"
                    record.event("output_parse_error", {"message": str(failure)})
                record.event("finish", {"output": output, "termination_reason": termination})
                break
            else:
                status, termination = "step_limit", "step_limit"
                record.event("step_limit", {"max_steps": max_steps})
        except Exception as failure:
            status = "budget_interrupted" if isinstance(failure, BudgetExceeded) else "error"
            termination = status
            if isinstance(failure, AERError):
                record.event("execution_error", {"error": failure.to_info().model_dump(mode="json")})
                error_info = failure.to_info().replace(stage=run_ctx.stage, operation_id=run_ctx.operation_id,
                                                       usage=record.usage())
            else:
                error = ExecutionError(str(failure) or type(failure).__name__, stage=run_ctx.stage,
                    operation_id=run_ctx.operation_id, plugin_id=self.plugin_id, usage=record.usage())
                error_info = error.to_info()
                record.event("execution_error", {"error": error_info.model_dump(mode="json")})
        usage = record.usage()
        if error_info is not None:
            error_info = error_info.replace(usage=usage)
        return ExecutionResult(task_id=task.task_id, agent_version_id=snapshot.version_id,
            operation_id=run_ctx.operation_id, output=output, trajectory=record.events,
            state_summary={"agent_plugin": self.plugin_id, "model_steps": steps,
                           "effective_max_steps": max_steps,
                           "termination_reason": termination, "fresh_session": True},
            usage=usage, status=status, error=error_info)


class SingleCallAgent(ReActAgent):
    plugin_id = "single_call_agent"
    uses_tools = False
