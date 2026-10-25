"""Explicit router and Prompt Evolver factories with minimal dependencies."""

from pathlib import Path

from aer_core.config.models import RoutingSettings
from aer_core.contracts.providers import ModelProvider
from aer_core.contracts.storage import ArtifactStore
from aer_core.plugin_registry import API_VERSION, PluginRegistry, ResolvedDependencies
from aer_core.schemas.plugins import PluginCapabilities, PluginMetadata
from plugins.evolution.prompt import PromptEvolverConfig, SimplePromptEvolver
from plugins.evolution.model_selection import ModelSelectionEvolverConfig, ModelSelectionEvolver
from plugins.routers.basic import FixedRouter, ManualRouter
from plugins.routers.llm import LLMRouter
from plugins.routers.jev import JevRouter, JevRoutingSettings, resolve_jev_credentials


def build_fixed(*, config: RoutingSettings, dependencies: ResolvedDependencies) -> FixedRouter:
    return FixedRouter(config)


def build_manual(*, config: RoutingSettings, dependencies: ResolvedDependencies) -> ManualRouter:
    return ManualRouter(config)


def build_prompt(*, config: PromptEvolverConfig, dependencies: ResolvedDependencies) -> SimplePromptEvolver:
    return SimplePromptEvolver(config, dependencies.get("artifact_store"), dependencies.get("model_provider"))


def build_llm_router(*, config: RoutingSettings, dependencies: ResolvedDependencies):
    return LLMRouter(config, dependencies.get("model_provider"), dependencies.get("artifact_store"))


def build_jev_router(*, config: JevRoutingSettings, dependencies: ResolvedDependencies):
    credentials = resolve_jev_credentials(config.config, dependencies.get("config_root"))
    return JevRouter(config, dependencies.get("artifact_store"), credentials)


def build_model_selection(*, config: ModelSelectionEvolverConfig, dependencies: ResolvedDependencies):
    return ModelSelectionEvolver(config, dependencies.get("artifact_store"), dependencies.get("model_provider"))


def register_evolution_plugins(registry: PluginRegistry) -> None:
    for plugin_id, mode, factory in [("fixed_router", "fixed", build_fixed), ("manual_router", "manual", build_manual)]:
        registry.register(PluginMetadata(id=plugin_id, category="router", api_version=API_VERSION,
            plugin_version="0.1.0", capabilities=PluginCapabilities(routing_modes=[mode])), factory, config_model=RoutingSettings)
    registry.register(PluginMetadata(id="simple_prompt_evolver", category="evolution", api_version=API_VERSION,
        plugin_version="0.1.0", capabilities=PluginCapabilities(targets=["prompt"], compatible_agents=["react_agent", "single_call_agent", "codex_cli_agent"]),
        dependencies=["artifact_store", "model_provider"]), build_prompt, config_model=PromptEvolverConfig,
        dependency_types={"artifact_store": ArtifactStore, "model_provider": ModelProvider})
    registry.register(PluginMetadata(id="llm_router", category="router", api_version=API_VERSION,
        plugin_version="0.1.0", capabilities=PluginCapabilities(routing_modes=["llm"]),
        dependencies=["model_provider", "artifact_store"]), build_llm_router, config_model=RoutingSettings,
        dependency_types={"model_provider": ModelProvider, "artifact_store": ArtifactStore})
    registry.register(PluginMetadata(id="jev_router", category="router", api_version=API_VERSION,
        plugin_version="0.1.0", capabilities=PluginCapabilities(routing_modes=["llm"]),
        dependencies=["artifact_store", "config_root"]), build_jev_router, config_model=JevRoutingSettings,
        dependency_types={"artifact_store": ArtifactStore, "config_root": Path})
    registry.register(PluginMetadata(id="model_selection_evolver", category="evolution", api_version=API_VERSION,
        plugin_version="0.1.0", capabilities=PluginCapabilities(targets=["model"], compatible_agents=["codex_cli_agent"]),
        dependencies=["artifact_store", "model_provider"]), build_model_selection, config_model=ModelSelectionEvolverConfig,
        dependency_types={"artifact_store": ArtifactStore, "model_provider": ModelProvider})
