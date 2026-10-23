"""Factories and explicit dependencies for the actual execution layer."""

from aer_core.config.models import AgentSettings, ModelProviderSettings, Settings, ToolProviderSettings
from aer_core.contracts.providers import ModelProvider, ToolProvider
from aer_core.contracts.storage import ArtifactStore
from aer_core.contracts.workspace import WorkspaceProvider, ResultCollector
from aer_core.plugin_registry import API_VERSION, PluginRegistry, ResolvedDependencies
from aer_core.schemas.plugins import PluginCapabilities, PluginMetadata
from aer_core.schemas.references import AgentSnapshot
from plugins.agents.react import ReActAgent, SingleCallAgent, LocalAgentSettings, snapshot_config, initial_artifacts
from plugins.agents.codex import CodexCliAgent, CodexAgentSettings
from plugins.agents import codex
from plugins.diagnosers.lightweight import LightweightDiagnoser
from plugins.diagnosers.llm import LLMDiagnoser
from plugins.providers.scripted import ScriptedModel
from plugins.providers.tools import DeterministicTools
from plugins.catalog import ScriptedModelConfig
from plugins.catalog import QwenModelConfig
from plugins.providers.qwen import QwenCredentials, QwenModel


class EmptyConfig(Settings):
    pass


def build_model(*, config: ScriptedModelConfig, dependencies: ResolvedDependencies) -> ScriptedModel:
    settings = dependencies.get("settings")
    if settings.config != config.model_dump(mode="json"):
        raise ValueError("Model settings do not match the configured scenario")
    return ScriptedModel(settings)


def build_tools(*, config: ToolProviderSettings, dependencies: ResolvedDependencies) -> DeterministicTools:
    return DeterministicTools(config)


def build_qwen(*, config: QwenModelConfig, dependencies: ResolvedDependencies) -> QwenModel:
    settings = dependencies.get("settings")
    if QwenModelConfig.model_validate(settings.config) != config:
        raise ValueError("Qwen settings do not match the provider configuration")
    return QwenModel(settings, dependencies.get("credentials"))


def build_react(*, config: AgentSettings, dependencies: ResolvedDependencies) -> ReActAgent:
    return ReActAgent(config, dependencies.get("initial_snapshot"), dependencies.get("artifact_store"),
                      dependencies.get("model_provider"), dependencies.get("tool_provider"))


def build_single(*, config: AgentSettings, dependencies: ResolvedDependencies) -> SingleCallAgent:
    return SingleCallAgent(config, dependencies.get("initial_snapshot"), dependencies.get("artifact_store"),
                           dependencies.get("model_provider"))


def build_codex(*, config: CodexAgentSettings, dependencies: ResolvedDependencies):
    return CodexCliAgent(config, dependencies.get("initial_snapshot"), dependencies.get("artifact_store"),
        workspace_provider=dependencies.get("workspace_provider"), result_collector=dependencies.get("result_collector"))


def build_diagnoser(*, config: EmptyConfig, dependencies: ResolvedDependencies) -> LightweightDiagnoser:
    return LightweightDiagnoser()


def build_llm_diagnoser(*, config: EmptyConfig, dependencies: ResolvedDependencies) -> LLMDiagnoser:
    return LLMDiagnoser(dependencies.get("model_provider"), dependencies.get("artifact_store"))


def register_execution_plugins(registry: PluginRegistry) -> None:
    registry.register(PluginMetadata(id="scripted_model", category="model_provider", api_version=API_VERSION,
        plugin_version="0.1.0", dependencies=["settings"]), build_model, config_model=ScriptedModelConfig,
        dependency_types={"settings": ModelProviderSettings})
    registry.register(PluginMetadata(id="qwen_model", category="model_provider", api_version=API_VERSION,
        plugin_version="0.1.0", dependencies=["settings", "credentials"]), build_qwen,
        config_model=QwenModelConfig, dependency_types={"settings": ModelProviderSettings, "credentials": QwenCredentials})
    registry.register(PluginMetadata(id="deterministic_tools", category="tool_provider", api_version=API_VERSION,
        plugin_version="0.1.0", capabilities=PluginCapabilities(tools=["calculator"])), build_tools, config_model=ToolProviderSettings)
    common = {"initial_snapshot": AgentSnapshot, "artifact_store": ArtifactStore, "model_provider": ModelProvider}
    for plugin_id, factory, deps, steps in [
        ("react_agent", build_react, {**common, "tool_provider": ToolProvider}, None),
        ("single_call_agent", build_single, common, 1),
    ]:
        registry.register(PluginMetadata(id=plugin_id, category="agent", api_version=API_VERSION,
            plugin_version="0.1.0", capabilities=PluginCapabilities(
                targets=["prompt"], fixed_max_steps=steps),
            dependencies=tuple(deps)), factory, config_model=LocalAgentSettings, dependency_types=deps,
            snapshot_config=snapshot_config, initial_artifacts=initial_artifacts)
    codex_deps = {"initial_snapshot": AgentSnapshot, "artifact_store": ArtifactStore,
                  "workspace_provider": WorkspaceProvider, "result_collector": ResultCollector}
    registry.register(PluginMetadata(id="codex_cli_agent", category="agent", api_version=API_VERSION,
        plugin_version="0.1.1", capabilities=PluginCapabilities(targets=["prompt", "model"]),
        dependencies=tuple(codex_deps)), build_codex, config_model=CodexAgentSettings, dependency_types=codex_deps,
        snapshot_config=codex.snapshot_config, initial_artifacts=codex.initial_artifacts)
    registry.register(PluginMetadata(id="lightweight_diagnoser", category="diagnoser", api_version=API_VERSION,
        plugin_version="0.1.0"), build_diagnoser, config_model=EmptyConfig)
    registry.register(PluginMetadata(id="llm_diagnoser", category="diagnoser", api_version=API_VERSION,
        plugin_version="0.1.0", dependencies=["model_provider", "artifact_store"]), build_llm_diagnoser,
        config_model=EmptyConfig, dependency_types={"model_provider": ModelProvider, "artifact_store": ArtifactStore})
