"""Serializable registration metadata, build plans and assembly manifests."""

from typing import Literal

from pydantic import Field, model_validator

from .base import DomainModel, Identifier, Items, JsonObject, PositiveInt, ReadOnlyMap


PluginCategory = Literal[
    "agent", "router", "evolution", "model_provider", "tool_provider",
    "benchmark", "evaluator", "diagnoser", "validation", "acceptance_policy",
    "artifact_store", "version_store", "experience_store", "runner",
    "workspace_provider", "result_collector",
]


class PluginCapabilities(DomainModel):
    targets: Items[Identifier] = ()
    compatible_agents: Items[Identifier] = ()
    tools: Items[Identifier] = ()
    splits: Items[Identifier] = ()
    benchmark_id: Identifier | None = None
    policy_versions: Items[Identifier] = ()
    routing_modes: Items[Literal["fixed", "manual", "llm"]] = ()
    fixed_max_steps: PositiveInt | None = None


class PluginMetadata(DomainModel):
    id: Identifier
    category: PluginCategory
    api_version: Identifier
    plugin_version: Identifier
    capabilities: PluginCapabilities = Field(default_factory=PluginCapabilities)
    dependencies: Items[Identifier] = ()

    @model_validator(mode="after")
    def unique_dependencies(self):
        if len(set(self.dependencies)) != len(self.dependencies):
            raise ValueError("dependency names must be unique")
        return self


class PluginDescriptor(DomainModel):
    metadata: PluginMetadata
    factory: Identifier
    config_schema: JsonObject


class PluginBinding(DomainModel):
    """Named instance; dependencies reference another binding or an external input."""

    name: Identifier
    category: PluginCategory
    plugin_id: Identifier
    config: JsonObject
    dependency_bindings: ReadOnlyMap[Identifier] = Field(default_factory=dict)


class PluginInstanceRecord(DomainModel):
    binding_name: Identifier
    descriptor: PluginDescriptor
    resolved_config: JsonObject
    dependency_bindings: ReadOnlyMap[Identifier]


class AssemblyManifest(DomainModel):
    schema_version: Literal["aer.assembly.v0.1"] = "aer.assembly.v0.1"
    api_version: Identifier
    instances: Items[PluginInstanceRecord]
