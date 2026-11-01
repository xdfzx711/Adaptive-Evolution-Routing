"""Explicit router and Prompt Evolver factories with minimal dependencies."""

from pathlib import Path

from aer_core.config.models import RoutingSettings
from aer_core.contracts.providers import ModelProvider
from aer_core.contracts.storage import ArtifactStore
from aer_core.contracts.workspace import EvolutionWorkspaceService
from aer_core.contracts.optimization import CandidateTrialService, ModelProviderFactory
from aer_core.progress import ProgressReporter
from aer_core.plugin_registry import API_VERSION, PluginRegistry, ResolvedDependencies
from aer_core.schemas.plugins import PluginCapabilities, PluginMetadata
from plugins.evolution.prompt import PromptEvolverConfig, SimplePromptEvolver
from plugins.evolution.model_selection import ModelSelectionEvolverConfig, ModelSelectionEvolver
from plugins.agents.model_selection import CodexModelAdapter
from plugins.agents.tool_selection import CodexToolAdapter
from plugins.evolution.tool_selection import ToolSelectionEvolver, ToolSelectionEvolverConfig
from plugins.evolution.gepa.config import GEPAPromptConfig
from plugins.evolution.oscopilot.config import OSCopilotConfig
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
    return ModelSelectionEvolver(config, dependencies.get("artifact_store"), dependencies.get("model_provider"),
        adapter=CodexModelAdapter())


def build_tool_selection(*, config: ToolSelectionEvolverConfig, dependencies: ResolvedDependencies):
    return ToolSelectionEvolver(config, dependencies.get("artifact_store"), dependencies.get("model_provider"),
        adapter=CodexToolAdapter())


def build_gepa(*, config: GEPAPromptConfig, dependencies: ResolvedDependencies):
    from plugins.evolution.gepa.evolver import GEPAPromptEvolver, require_gepa
    require_gepa()
    trials = dependencies.get("candidate_trials")
    if config.trial_evaluator is not None:
        from aer_core.errors import PluginConfigurationError
        configure = getattr(trials, "with_evaluator", None)
        if not callable(configure):
            raise PluginConfigurationError("Configured trial service cannot select a separate evaluator", stage="setup",
                plugin_id="gepa_prompt_evolver")
        trials = configure(config.trial_evaluator)
    model = dependencies.get("model_factory").create(config.reflection_model)
    return GEPAPromptEvolver(config, dependencies.get("artifact_store"), model,
                            trials, dependencies.get("optimization_progress"))


def build_oscopilot(*, config: OSCopilotConfig, dependencies: ResolvedDependencies):
    from plugins.evolution.oscopilot.evolver import OSCopilotToolEvolver
    factory = dependencies.get("model_factory")
    generator = factory.create(config.generation_model) if config.generation_model else dependencies.get("model_provider")
    judge = factory.create(config.judge_model) if config.judge_model else generator
    return OSCopilotToolEvolver(config, dependencies.get("artifact_store"), generator, judge,
        dependencies.get("evolution_workspace"), dependencies.get("optimization_progress"),
        config_root=dependencies.get("config_root"))


def register_evolution_plugins(registry: PluginRegistry) -> None:
    registry.register(PluginMetadata(id="oscopilot_tool_evolver", category="evolution", api_version=API_VERSION,
        plugin_version="0.1.0", capabilities=PluginCapabilities(targets=["tool"], compatible_agents=["codex_cli_agent"]),
        dependencies=["artifact_store", "model_provider", "model_factory", "evolution_workspace", "optimization_progress", "config_root"]),
        build_oscopilot, config_model=OSCopilotConfig,
        dependency_types={"artifact_store": ArtifactStore, "model_provider": ModelProvider,
                          "model_factory": ModelProviderFactory, "evolution_workspace": EvolutionWorkspaceService,
                          "optimization_progress": ProgressReporter, "config_root": Path})
    for plugin_id, mode, factory in [("fixed_router", "fixed", build_fixed), ("manual_router", "manual", build_manual)]:
        registry.register(PluginMetadata(id=plugin_id, category="router", api_version=API_VERSION,
            plugin_version="0.1.0", capabilities=PluginCapabilities(routing_modes=[mode])), factory, config_model=RoutingSettings)
    registry.register(PluginMetadata(id="simple_prompt_evolver", category="evolution", api_version=API_VERSION,
        plugin_version="0.1.0", capabilities=PluginCapabilities(targets=["prompt"], compatible_agents=["react_agent", "single_call_agent", "codex_cli_agent"]),
        dependencies=["artifact_store", "model_provider"]), build_prompt, config_model=PromptEvolverConfig,
        dependency_types={"artifact_store": ArtifactStore, "model_provider": ModelProvider})
    registry.register(PluginMetadata(id="gepa_prompt_evolver", category="evolution", api_version=API_VERSION,
        plugin_version="0.1.0", capabilities=PluginCapabilities(targets=["prompt"],
            compatible_agents=["react_agent", "single_call_agent", "codex_cli_agent"]),
        dependencies=["artifact_store", "model_factory", "candidate_trials", "optimization_progress"]),
        build_gepa, config_model=GEPAPromptConfig,
        dependency_types={"artifact_store": ArtifactStore, "model_factory": ModelProviderFactory,
                          "candidate_trials": CandidateTrialService, "optimization_progress": ProgressReporter})
    registry.register(PluginMetadata(id="llm_router", category="router", api_version=API_VERSION,
        plugin_version="0.1.0", capabilities=PluginCapabilities(routing_modes=["llm"]),
        dependencies=["model_provider", "artifact_store"]), build_llm_router, config_model=RoutingSettings,
        dependency_types={"model_provider": ModelProvider, "artifact_store": ArtifactStore})
    registry.register(PluginMetadata(id="jev_router", category="router", api_version=API_VERSION,
        plugin_version="0.1.0", capabilities=PluginCapabilities(routing_modes=["llm"]),
        dependencies=["artifact_store", "config_root"]), build_jev_router, config_model=JevRoutingSettings,
        dependency_types={"artifact_store": ArtifactStore, "config_root": Path})
    registry.register(PluginMetadata(id="model_selection_evolver", category="evolution", api_version=API_VERSION,
        plugin_version="0.2.0", capabilities=PluginCapabilities(targets=["model"], compatible_agents=["codex_cli_agent"]),
        dependencies=["artifact_store", "model_provider"]), build_model_selection, config_model=ModelSelectionEvolverConfig,
        dependency_types={"artifact_store": ArtifactStore, "model_provider": ModelProvider})
    registry.register(PluginMetadata(id="tool_selection_evolver", category="evolution", api_version=API_VERSION,
        plugin_version="0.1.0", capabilities=PluginCapabilities(targets=["tool"], compatible_agents=["codex_cli_agent"]),
        dependencies=["artifact_store", "model_provider"]), build_tool_selection, config_model=ToolSelectionEvolverConfig,
        dependency_types={"artifact_store": ArtifactStore, "model_provider": ModelProvider})
