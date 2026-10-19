"""Validate a dependency graph before creating synchronous plugin instances."""

import inspect
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

from pydantic import ValidationError

from aer_core.config.validation import schema_issues
from aer_core.errors import (
    ContractError, DependencyError, PluginConfigurationError, PluginError,
    PluginInstantiationError, PluginVersionError,
)
from aer_core.plugin_registry import CONTRACTS, PluginRegistry, ResolvedDependencies
from aer_core.schemas.evolution import AgentCapabilities, EvolutionCapabilities
from aer_core.schemas.plugins import AssemblyManifest, PluginBinding, PluginInstanceRecord


@dataclass(frozen=True)
class BuiltPlugins:
    instances: Mapping[str, object]
    manifest: AssemblyManifest

    def __post_init__(self):
        object.__setattr__(self, "instances", MappingProxyType(dict(self.instances)))


def _check_contract(instance: object, contract: type, plugin_id: str) -> None:
    for name in getattr(contract, "__annotations__", {}):
        if not hasattr(instance, name):
            raise ContractError(f"{plugin_id} must declare {name}", stage="setup", plugin_id=plugin_id)
    for name, expected in inspect.getmembers(contract, inspect.isfunction):
        if name.startswith("_"):
            continue
        method = getattr(instance, name, None)
        if not callable(method) or inspect.iscoroutinefunction(method) or inspect.isgeneratorfunction(method):
            raise ContractError(f"{plugin_id}.{name} must be a synchronous callable", stage="setup", plugin_id=plugin_id)
        parameters = list(inspect.signature(expected).parameters.values())[1:]
        positional = [None for item in parameters if item.kind in {item.POSITIONAL_ONLY, item.POSITIONAL_OR_KEYWORD}]
        keywords = {item.name: None for item in parameters if item.kind == item.KEYWORD_ONLY}
        named = {item.name: None for item in parameters if item.kind in {item.POSITIONAL_OR_KEYWORD, item.KEYWORD_ONLY}}
        try:
            signature = inspect.signature(method)
            signature.bind(*positional, **keywords)
            signature.bind(**named)
        except (TypeError, ValueError) as error:
            raise ContractError(f"{plugin_id}.{name} has an incompatible signature; expected {inspect.signature(expected)}", stage="setup", plugin_id=plugin_id) from error


def _check_dependency(value: object, expected: type, label: str, plugin_id: str) -> None:
    if not isinstance(value, expected):
        raise DependencyError(f"{label} requires {expected.__name__}; received {type(value).__name__}", stage="setup", plugin_id=plugin_id)
    if getattr(expected, "_is_protocol", False):
        try:
            _check_contract(value, expected, plugin_id)
        except ContractError as error:
            raise DependencyError(f"{label} has an incompatible dependency interface: {error}", stage="setup", plugin_id=plugin_id) from error


class PluginManager:
    def __init__(self, registry: PluginRegistry):
        self.registry = registry

    def build(
        self, bindings: Sequence[PluginBinding], *, dependencies: Mapping[str, object] | None = None,
    ) -> BuiltPlugins:
        external = dict(dependencies or {})
        plan = {binding.name: binding for binding in bindings}
        if len(plan) != len(bindings):
            raise DependencyError("binding names must be unique", stage="setup")
        if set(plan) & set(external):
            raise DependencyError("binding names must not collide with external dependency names", stage="setup")
        entries, configurations = {}, {}
        for binding in bindings:
            entry = self.registry.get(binding.category, binding.plugin_id)
            if entry.metadata.api_version != self.registry.api_version:
                raise PluginVersionError("registered plugin API no longer matches the registry", stage="setup", plugin_id=binding.plugin_id)
            entries[binding.name] = entry
            try:
                configurations[binding.name] = entry.config_model.model_validate(binding.model_dump()["config"], strict=True, extra="forbid")
            except ValidationError as error:
                detail = "; ".join(f"{item.field}: {item.message}" for item in schema_issues(error, binding.name + ".config"))
                raise PluginConfigurationError(detail, stage="setup", plugin_id=binding.plugin_id) from error
            if set(binding.dependency_bindings) != set(entry.metadata.dependencies):
                raise DependencyError(f"{binding.name} must bind exactly these dependencies: {', '.join(entry.metadata.dependencies) or '(none)'}", stage="setup", plugin_id=binding.plugin_id)
            for name, source in binding.dependency_bindings.items():
                if source not in plan and source not in external:
                    raise DependencyError(f"{binding.name}.{name} references missing dependency {source!r}", stage="setup", plugin_id=binding.plugin_id)
                expected_type = entry.dependency_types[name]
                if source in external:
                    _check_dependency(external[source], expected_type, f"{binding.name}.{name}", binding.plugin_id)

        order: list[str] = []
        visiting: list[str] = []
        visited: set[str] = set()

        def visit(name: str):
            if name in visiting:
                raise DependencyError("cyclic plugin dependencies: " + " -> ".join([*visiting, name]), stage="setup", plugin_id=plan[name].plugin_id)
            if name in visited:
                return
            visiting.append(name)
            for source in plan[name].dependency_bindings.values():
                if source in plan:
                    visit(source)
            visiting.pop()
            visited.add(name)
            order.append(name)

        for name in plan:
            visit(name)

        instances: dict[str, object] = {}
        records = []
        for name in order:
            binding, entry = plan[name], entries[name]
            resolved = {}
            for dependency_name, source in binding.dependency_bindings.items():
                value = instances[source] if source in instances else external[source]
                expected = entry.dependency_types[dependency_name]
                _check_dependency(value, expected, f"{name}.{dependency_name}", binding.plugin_id)
                resolved[dependency_name] = value
            try:
                instance = entry.factory(config=configurations[name], dependencies=ResolvedDependencies(resolved))
            except PluginError:
                raise
            except Exception as error:
                raise PluginInstantiationError(f"factory failed for {binding.category}/{binding.plugin_id}: {error}", stage="setup", plugin_id=binding.plugin_id) from error
            if inspect.isawaitable(instance) or inspect.isgenerator(instance):
                close = getattr(instance, "close", None)
                if callable(close):
                    close()
                raise ContractError("factory returned an asynchronous/generator result", stage="setup", plugin_id=binding.plugin_id)
            _check_contract(instance, CONTRACTS[binding.category], binding.plugin_id)
            try:
                if binding.category in {"agent", "evolution"}:
                    actual = instance.capabilities()
                    expected = AgentCapabilities if binding.category == "agent" else EvolutionCapabilities
                    if not isinstance(actual, expected):
                        raise TypeError(f"capabilities() must return {expected.__name__}")
                    if set(actual.supported_targets) != set(entry.metadata.capabilities.targets):
                        raise ValueError("runtime targets differ from registered capabilities")
                    if isinstance(actual, EvolutionCapabilities) and set(actual.compatible_agents) != set(entry.metadata.capabilities.compatible_agents):
                        raise ValueError("runtime compatible_agents differ from registered capabilities")
            except Exception as error:
                raise ContractError(f"invalid capabilities for {binding.plugin_id}: {error}", stage="setup", plugin_id=binding.plugin_id) from error
            instances[name] = instance
            records.append(PluginInstanceRecord(binding_name=name, descriptor=entry.descriptor(), resolved_config=configurations[name].model_dump(mode="json"), dependency_bindings=dict(binding.dependency_bindings)))
        return BuiltPlugins(instances, AssemblyManifest(api_version=self.registry.api_version, instances=records))
