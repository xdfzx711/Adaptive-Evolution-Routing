"""Compose explicit disclosure declarations without interpreting task inputs."""

from aer_core.context import ContextBuilder
from aer_core.errors import ContractError
from aer_core.schemas.feedback import ContextPolicy, TaskFeedbackPolicy
from aer_core.scoring_feedback import scorer_feedback_policy


def context_builder(execution, task, *, history_max_attempts=5, history_max_text_chars=320, history_max_change_chars=4000,
                    history_max_construction_chars=16000):
    declaration = execution.benchmark.benchmark.feedback_policy(task)
    if not isinstance(declaration, TaskFeedbackPolicy) or declaration.task != task.identity:
        raise ContractError("Benchmark feedback policy must match the selected task", stage="setup")
    capabilities = execution.agent.capabilities()
    tools = execution.built_plugins.instances.get("tools")
    specs = tools.specifications() if tools is not None else ()
    policy = ContextPolicy(task=task.identity, output_fields=declaration.output_fields,
        **scorer_feedback_policy(execution.benchmark.evaluator,
            execution.benchmark.loaded_config.config.assessment.evaluator),
        metadata_fields=declaration.metadata_fields, allow_evolution=declaration.allow_evolution,
        reason_messages={**declaration.reason_messages, **getattr(execution.benchmark.evaluator, "public_reason_messages", {})},
        artifact_kinds=capabilities.supported_artifact_kinds,
        state_fields=capabilities.feedback_state_fields, snapshot_config_fields=capabilities.feedback_config_fields,
        history_max_attempts=history_max_attempts, history_max_text_chars=history_max_text_chars,
        history_max_change_chars=history_max_change_chars,
        history_max_construction_chars=history_max_construction_chars,
        tool_arguments={**dict(capabilities.feedback_tool_arguments), **{tool.name: tool.feedback_arguments for tool in specs}},
        tool_outputs={**dict(capabilities.feedback_tool_outputs), **{tool.name: tool.feedback_outputs for tool in specs}})
    return ContextBuilder(execution.storage.experience_store, policy)
