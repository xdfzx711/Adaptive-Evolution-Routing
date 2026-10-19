"""Explicit registration; no discovery, imports or factory execution here."""

import inspect
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol

from pydantic import BaseModel

from aer_core.config.catalog import ConfigCatalog, PluginDeclaration
from aer_core.config.models import ExperimentConfig
from aer_core.contracts.agent import AgentPlugin
from aer_core.contracts.benchmark import BenchmarkPlugin
from aer_core.contracts.diagnoser import DiagnoserPlugin
from aer_core.contracts.evaluator import EvaluatorPlugin
from aer_core.contracts.evolution import EvolutionPlugin
from aer_core.contracts.providers import ModelProvider, ToolProvider
from aer_core.contracts.router import RouterPlugin
from aer_core.contracts.runner import ExperimentRunner
from aer_core.contracts.storage import ArtifactStore, ExperienceStore, VersionStore
from aer_core.contracts.validation import AcceptancePolicy, ValidationPlugin
from aer_core.errors import DuplicatePluginError, PluginNotFound, PluginRegistrationError, PluginVersionError
from aer_core.schemas.plugins import PluginDescriptor, PluginMetadata


API_VERSION = "1.0"
CONTRACTS = MappingProxyType({
    "agent": AgentPlugin, "router": RouterPlugin, "evolution": EvolutionPlugin,
    "model_provider": ModelProvider, "tool_provider": ToolProvider,
    "benchmark": BenchmarkPlugin, "evaluator": EvaluatorPlugin,
    "diagnoser": DiagnoserPlugin, "validation": ValidationPlugin,
    "acceptance_policy": AcceptancePolicy, "artifact_store": ArtifactStore,
    "version_store": VersionStore, "experience_store": ExperienceStore,
    "runner": ExperimentRunner,
})


@dataclass(frozen=True)
class ResolvedDependencies:
    """Runtime-only dependency objects; these are never serialized."""

    values: Mapping[str, object]

    def __post_init__(self):
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))

    def get(self, name: str):
        return self.values[name]


class PluginFactory(Protocol):
    def __call__(self, *, config: BaseModel, dependencies: ResolvedDependencies) -> object: ...


@dataclass(frozen=True)
class RegisteredPlugin:
    metadata: PluginMetadata
    factory: PluginFactory
    config_model: type[BaseModel]
    dependency_types: Mapping[str, type]
    snapshot_config: Callable[[ExperimentConfig], dict] | None = None
    initial_artifacts: Callable[[ExperimentConfig], dict[str, bytes]] | None = None

    def descriptor(self) -> PluginDescriptor:
        factory = self.factory
        name = f"{getattr(factory, '__module__', type(factory).__module__)}.{getattr(factory, '__qualname__', type(factory).__qualname__)}"
        return PluginDescriptor(metadata=self.metadata, factory=name, config_schema=self.config_model.model_json_schema())


class PluginRegistry:
    def __init__(self, *, api_version: str = API_VERSION):
        self.api_version = api_version
        self._entries: dict[tuple[str, str], RegisteredPlugin] = {}

    def register(
        self, metadata: PluginMetadata, factory: PluginFactory, *,
        config_model: type[BaseModel], dependency_types: Mapping[str, type] | None = None,
        snapshot_config: Callable[[ExperimentConfig], dict] | None = None,
        initial_artifacts: Callable[[ExperimentConfig], dict[str, bytes]] | None = None,
    ) -> None:
        key = (metadata.category, metadata.id)
        if key in self._entries:
            raise DuplicatePluginError(f"duplicate plugin registration: {metadata.category}/{metadata.id}", stage="setup", plugin_id=metadata.id)
        if metadata.api_version != self.api_version:
            raise PluginVersionError(f"{metadata.category}/{metadata.id} requires API {metadata.api_version}; registry supports {self.api_version}", stage="setup", plugin_id=metadata.id)
        if not re.fullmatch(r"\d+\.\d+", metadata.api_version) or not re.fullmatch(r"\d+\.\d+\.\d+", metadata.plugin_version):
            raise PluginVersionError("api_version must be major.minor and plugin_version must be major.minor.patch", stage="setup", plugin_id=metadata.id)
        call = getattr(factory, "__call__", None)
        if not callable(factory) or any(inspect.iscoroutinefunction(value) or inspect.isgeneratorfunction(value) for value in (factory, call)):
            raise PluginRegistrationError("factory must be a synchronous callable", stage="setup", plugin_id=metadata.id)
        try:
            inspect.signature(factory).bind(config=None, dependencies=None)
        except (TypeError, ValueError) as error:
            raise PluginRegistrationError("factory must accept config and dependencies as keyword arguments", stage="setup", plugin_id=metadata.id) from error
        if not isinstance(config_model, type) or not issubclass(config_model, BaseModel):
            raise PluginRegistrationError("config_model must be a Pydantic model class", stage="setup", plugin_id=metadata.id)
        try:
            config_model.model_json_schema()
        except Exception as error:
            raise PluginRegistrationError("config_model must provide a serializable JSON schema", stage="setup", plugin_id=metadata.id) from error
        types = dict(dependency_types or {})
        if set(types) != set(metadata.dependencies) or any(not isinstance(value, type) for value in types.values()):
            raise PluginRegistrationError("dependency_types must supply one runtime type/Protocol per declared dependency", stage="setup", plugin_id=metadata.id)
        if any(getattr(value, "_is_protocol", False) and not getattr(value, "_is_runtime_protocol", False) for value in types.values()):
            raise PluginRegistrationError("dependency Protocols must be runtime_checkable", stage="setup", plugin_id=metadata.id)
        if snapshot_config is not None and (metadata.category != "agent" or not callable(snapshot_config)):
            raise PluginRegistrationError("snapshot_config must be an Agent configuration builder", stage="setup", plugin_id=metadata.id)
        if snapshot_config is not None:
            if inspect.iscoroutinefunction(snapshot_config) or inspect.isgeneratorfunction(snapshot_config):
                raise PluginRegistrationError("snapshot_config must be synchronous", stage="setup", plugin_id=metadata.id)
            try:
                inspect.signature(snapshot_config).bind(None)
            except (TypeError, ValueError) as error:
                raise PluginRegistrationError("snapshot_config must accept one experiment configuration", stage="setup", plugin_id=metadata.id) from error
        if initial_artifacts is not None:
            if (metadata.category != "agent" or not callable(initial_artifacts) or
                inspect.iscoroutinefunction(initial_artifacts) or inspect.isgeneratorfunction(initial_artifacts)):
                raise PluginRegistrationError("initial_artifacts must be a synchronous Agent artifact builder", stage="setup", plugin_id=metadata.id)
            try:
                inspect.signature(initial_artifacts).bind(None)
            except (TypeError, ValueError) as error:
                raise PluginRegistrationError("initial_artifacts must accept one experiment configuration", stage="setup", plugin_id=metadata.id) from error
        self._entries[key] = RegisteredPlugin(metadata, factory, config_model, MappingProxyType(types), snapshot_config, initial_artifacts)

    def get(self, category: str, plugin_id: str) -> RegisteredPlugin:
        try:
            return self._entries[(category, plugin_id)]
        except KeyError as error:
            names = ", ".join(sorted(name for kind, name in self._entries if kind == category)) or "(none)"
            raise PluginNotFound(f"no factory registered for {category}/{plugin_id}; available: {names}", stage="setup", plugin_id=plugin_id) from error

    def descriptors(self) -> tuple[PluginDescriptor, ...]:
        return tuple(self._entries[key].descriptor() for key in sorted(self._entries))

    def configuration_catalog(self, base: ConfigCatalog | None = None) -> ConfigCatalog:
        declarations = {(item.category, item.plugin_id): item for item in base.declarations} if base else {}
        for entry in self._entries.values():
            meta, caps = entry.metadata, entry.metadata.capabilities
            declarations[(meta.category, meta.id)] = PluginDeclaration(
                meta.category, meta.id, targets=caps.targets,
                compatible_agents=caps.compatible_agents, tools=caps.tools,
                splits=caps.splits, benchmark_id=caps.benchmark_id,
                policy_versions=caps.policy_versions,
                config_model=entry.config_model if meta.category in {"model_provider", "evolution"} else None,
                routing_modes=caps.routing_modes, fixed_max_steps=caps.fixed_max_steps,
            )
        return ConfigCatalog(tuple(declarations.values()))
