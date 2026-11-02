"""Explicit plugin imports and dependency injection at the startup boundary."""

from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path

from aer_core.config import LoadedConfig, load_config
from aer_core.config.models import ExperimentConfig
from aer_core.contracts.bundle import ControllerPlugins
from aer_core.contracts.storage import ArtifactStore
from aer_core.errors import DependencyError
from aer_core.plugin_manager import BuiltPlugins, PluginManager
from aer_core.plugin_registry import PluginRegistry
from aer_core.schemas.plugins import PluginBinding
from aer_core.schemas.references import AgentSnapshot
from plugins.catalog import v0_1_catalog
from plugins.mocks import MockArtifactStore, register_mock_plugins
from plugins.storage.registration import register_local_storage
from plugins.benchmarks.registration import register_local_benchmark
from plugins.execution_registration import register_execution_plugins
from plugins.evolution_registration import register_evolution_plugins
from plugins.validation.registration import register_validation_plugins


def create_registry() -> PluginRegistry:
    registry = PluginRegistry()
    register_mock_plugins(registry)
    register_local_storage(registry)
    register_local_benchmark(registry)
    register_execution_plugins(registry)
    register_evolution_plugins(registry)
    register_validation_plugins(registry)
    from plugins.workspace.registration import register_workspace_plugins
    from plugins.benchmarks.swebench import register_swebench
    register_workspace_plugins(registry)
    register_swebench(registry)
    from plugins.evaluators.registration import register_evaluator_plugins
    register_evaluator_plugins(registry)
    return registry


def evolution_bindings(config: ExperimentConfig, registry: PluginRegistry) -> list[PluginBinding]:
    if config.evolution.mode == "disabled":
        return []
    bindings = [component_binding("router", "router", config.routing.plugin, config.routing.model_dump(mode="json"), registry)]
    for target, plugin_id in config.targets.default_operators.items():
        bindings.append(component_binding(f"operator:{target}", "evolution", plugin_id,
            {**config.targets.operator_configs.get(target, {}), "max_candidates": config.evolution.max_candidates_per_round}, registry))
    return bindings


def component_binding(name, category, plugin_id, config, registry):
    aliases = {"initial_snapshot": "initial_snapshot", "artifact_store": "artifact_store",
               "model_provider": "model", "tool_provider": "tools"}
    dependencies = registry.get(category, plugin_id).metadata.dependencies
    return PluginBinding(name=name, category=category, plugin_id=plugin_id, config=config,
                         dependency_bindings={key: aliases.get(key, key) for key in dependencies})


def with_providers(config, registry, bindings):
    """Add shared providers required by the whole selected dependency graph."""
    result = list(bindings)
    while True:
        names = {binding.name for binding in result}
        required = {source for binding in result for source in binding.dependency_bindings.values()}
        additions = []
        if "model" in required - names:
            entry = registry.get("model_provider", config.providers.model.plugin)
            aliases = {"settings": "model_settings", "credentials": "model_credentials", "tool_provider": "tools"}
            additions.append(PluginBinding(name="model", category="model_provider", plugin_id=config.providers.model.plugin,
                config=config.providers.model.config,
                dependency_bindings={key: aliases.get(key, key) for key in entry.metadata.dependencies}))
        if "tools" in required - names:
            additions.append(component_binding("tools", "tool_provider", config.providers.tools.plugin,
                             config.providers.tools.model_dump(mode="json"), registry))
        for name, field, category, default in (
            ("workspace_provider", "workspace", "workspace_provider", "empty_workspace"),
            ("result_collector", "collector", "result_collector", "json_result_collector"),
        ):
            if name in required - names:
                selection = config.agent.config.get(field, {"plugin": default})
                additions.append(component_binding(name, category, selection["plugin"], selection.get("config", {}), registry))
        if not additions:
            return result
        result = additions + result


def agent_bindings(config: ExperimentConfig, registry: PluginRegistry) -> list[PluginBinding]:
    return with_providers(config, registry, [component_binding("agent", "agent", config.agent.plugin,
        config.agent.model_dump(mode="json"), registry)])


def controlled_snapshot_config(config, registry):
    builder = registry.get("agent", config.agent.plugin).snapshot_config
    return builder(config) if builder else {"max_steps": config.agent.max_steps}


def initial_artifact_refs(config, registry, store):
    """Agent-owned initial components; startup never interprets their contents."""
    builder = registry.get("agent", config.agent.plugin).initial_artifacts
    contents = builder(config) if builder else {"prompt": config.agent.initial_prompt.encode("utf-8")}
    if not isinstance(contents, dict) or not contents or any(
        not isinstance(kind, str) or not kind.strip() or not isinstance(content, bytes)
        for kind, content in contents.items()
    ):
        raise DependencyError("Agent initial_artifacts must return non-empty bytes keyed by component kind", stage="setup", plugin_id=config.agent.plugin)
    return {kind: store.put(content, kind) for kind, content in contents.items()}


@dataclass(frozen=True)
class AssembledExperiment:
    loaded_config: LoadedConfig
    plugins: ControllerPlugins
    built_plugins: BuiltPlugins

    def as_dict(self) -> dict:
        return {
            "status": "assembled",
            "scope": "agent_router_evolver",
            "source_path": str(self.loaded_config.source_path),
            "mode": self.loaded_config.config.evolution.mode,
            "assembly_manifest": self.built_plugins.manifest.model_dump(mode="json"),
            "notes": [
                "Assembly constructs the selected plugins without executing a task or evolution.",
                "This command only assembles Agent/Router/Evolver; use inspect-benchmark and init-storage for other implemented components.",
            ],
        }


def assemble_experiment(
    path: str | Path, *, registry: PluginRegistry | None = None,
    dependencies: Mapping[str, object] | None = None,
    benchmark_index: str | Path | None = None,
) -> AssembledExperiment:
    registry = registry if registry is not None else create_registry()
    loaded = load_config(path, catalog=registry.configuration_catalog(v0_1_catalog()), benchmark_index=benchmark_index)
    from .configuration import resolve_skills
    loaded = resolve_skills(loaded)
    from .environment import provider_dependencies, resolve_model
    loaded = resolve_model(loaded, required=True)
    config = loaded.config
    bindings = agent_bindings(config, registry)
    bindings.extend(evolution_bindings(config, registry))
    bindings = with_providers(config, registry, bindings)

    # Catch unavailable real implementations before allocating fixture inputs.
    for binding in bindings:
        registry.get(binding.category, binding.plugin_id)
    external = dict(dependencies or {})
    for name, value in provider_dependencies(loaded, bindings, supplied=external).items():
        external.setdefault(name, value)
    if "initial_snapshot" not in external:
        if config.agent.initial_snapshot_ref is not None:
            raise DependencyError("supply initial_snapshot and artifact_store explicitly for frozen assembly, or use execute-task", stage="setup", plugin_id=config.agent.plugin)
        store = external.setdefault("artifact_store", MockArtifactStore())
        if not isinstance(store, ArtifactStore):
            raise DependencyError("artifact_store must implement ArtifactStore", stage="setup")
        artifacts = initial_artifact_refs(config, registry, store)
        controlled = controlled_snapshot_config(config, registry)
        identity = json.dumps({"agent": config.agent.plugin, "config": controlled,
                               "artifacts": {kind: ref.digest for kind, ref in artifacts.items()}}, sort_keys=True)
        external["initial_snapshot"] = AgentSnapshot(version_id="assembly-initial-" + sha256(identity.encode()).hexdigest(), agent_plugin_id=config.agent.plugin, config=controlled, artifacts=artifacts)
    snapshot = external["initial_snapshot"]
    if (not isinstance(snapshot, AgentSnapshot) or snapshot.agent_plugin_id != config.agent.plugin
        or snapshot.model_dump(mode="json")["config"] != controlled_snapshot_config(config, registry)):
        raise DependencyError("Snapshot controlled configuration does not match the selected Agent", stage="setup", plugin_id=config.agent.plugin)
    from .optimization import ConfiguredModelFactory, IsolatedCandidateTrials
    from aer_core.progress import NullProgressReporter
    external.setdefault("model_factory", ConfiguredModelFactory(loaded, registry))
    external.setdefault("candidate_trials", IsolatedCandidateTrials(loaded, registry, external.get("artifact_store"), ()))
    external.setdefault("optimization_progress", NullProgressReporter())
    from .evolution_workspace import ConfiguredEvolutionWorkspaces
    external.setdefault("evolution_workspace", ConfiguredEvolutionWorkspaces(loaded, registry))
    built = PluginManager(registry).build(bindings, dependencies=external)
    bundle = ControllerPlugins(agent=built.instances["agent"], router=built.instances.get("router"), operators={target: built.instances[f"operator:{target}"] for target in config.targets.default_operators})
    return AssembledExperiment(loaded, bundle, built)
