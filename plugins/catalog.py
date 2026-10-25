"""Names reserved by the v0.1 design; this module instantiates no plugins."""

from typing import Literal

from aer_core.config.catalog import ConfigCatalog, PluginDeclaration
from aer_core.config.models import NonBlank, Settings
from pydantic import Field
from typing import Annotated


class ScriptedModelConfig(Settings):
    scenario_id: Literal["mvp_v0_1_contract"]


class QwenModelConfig(Settings):
    api_key_env: NonBlank = "QWEN_API_KEY"
    base_url_env: NonBlank = "QWEN_BASE_URL"
    dotenv_path: NonBlank | None = None
    temperature: Annotated[float, Field(ge=0, le=2, allow_inf_nan=False)] = 0.0
    enable_thinking: bool | None = False
    send_seed: bool = False
    response_format: Literal["json_object"] | None = None


def v0_1_catalog() -> ConfigCatalog:
    return ConfigCatalog((
        PluginDeclaration("runner", "aer"),
        PluginDeclaration("benchmark", "toy_tasks", splits=("dev", "validation", "test"), benchmark_id="toy_tasks"),
        PluginDeclaration("benchmark", "hard_local", splits=("dev", "validation", "test"), benchmark_id="hard_local"),
        PluginDeclaration("agent", "react_agent", targets=("prompt",)),
        PluginDeclaration("agent", "codex_cli_agent", targets=("prompt", "model")),
        PluginDeclaration("agent", "single_call_agent", targets=("prompt",), fixed_max_steps=1),
        PluginDeclaration("model_provider", "scripted_model", config_model=ScriptedModelConfig),
        PluginDeclaration("model_provider", "qwen_model", config_model=QwenModelConfig),
        PluginDeclaration("tool_provider", "deterministic_tools", tools=("calculator",)),
        PluginDeclaration("evaluator", "deterministic_evaluator"),
        PluginDeclaration("diagnoser", "lightweight_diagnoser"),
        PluginDeclaration("diagnoser", "llm_diagnoser"),
        PluginDeclaration("router", "fixed_router", routing_modes=("fixed",)),
        PluginDeclaration("router", "manual_router", routing_modes=("manual",)),
        PluginDeclaration("router", "llm_router", routing_modes=("llm",)),
        PluginDeclaration("router", "jev_router", routing_modes=("llm",)),
        PluginDeclaration("evolution", "simple_prompt_evolver", targets=("prompt",), compatible_agents=("react_agent", "single_call_agent", "codex_cli_agent")),
        PluginDeclaration("evolution", "model_selection_evolver", targets=("model",), compatible_agents=("codex_cli_agent",)),
        PluginDeclaration("validation", "paired_validation"),
        PluginDeclaration("acceptance_policy", "improvement_with_regression_guard", policy_versions=("v0.1",)),
        PluginDeclaration("experience_store", "jsonl"),
        PluginDeclaration("artifact_store", "local_files"),
        PluginDeclaration("version_store", "local_manifest"),
    ))
