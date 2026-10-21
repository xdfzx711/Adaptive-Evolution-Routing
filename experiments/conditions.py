"""Private execution/scoring conditions shared by initial runs and validation."""

from plugins.storage.layout import compact_plugins


def validation_conditions(execution):
    config, benchmark = execution.benchmark.loaded_config.config, execution.benchmark
    model = execution.built_plugins.instances.get("model")
    tasks = (*benchmark.execution_tasks, *benchmark.similar_tasks, *benchmark.regression_tasks)
    return {"schema_version": "aer.validation.conditions.v0.1", "dataset_version": benchmark.benchmark.dataset_version,
        "scorer_version": benchmark.evaluator.scorer_version,
        "execution_plugins": {"plugins": compact_plugins(execution.built_plugins.manifest)},
        "benchmark_plugins": {"plugins": compact_plugins(benchmark.built_plugins.manifest)},
        "execution_plugin_configs": {item.binding_name: item.model_dump(mode="json")["resolved_config"]
                                     for item in execution.built_plugins.manifest.instances},
        "benchmark_plugin_configs": {item.binding_name: item.model_dump(mode="json")["resolved_config"]
                                     for item in benchmark.built_plugins.manifest.instances},
        "model_runtime": model.runtime_info() if callable(getattr(model, "runtime_info", None)) else None,
        "agent_runtime": execution.agent.runtime_info() if callable(getattr(execution.agent, "runtime_info", None)) else None,
        "model_endpoint": getattr(getattr(model, "credentials", None), "base_url", None),
        "model_timeout_seconds": config.limits.model_call_timeout_seconds,
        "tool_timeout_seconds": config.limits.tool_call_timeout_seconds,
        "max_output_tokens": config.limits.max_output_tokens_per_call,
        "tasks": [item.model_dump(mode="json") for item in tasks],
        "scoring_specs": [benchmark.benchmark.evaluation_spec(item).model_dump(mode="json") for item in tasks]}
