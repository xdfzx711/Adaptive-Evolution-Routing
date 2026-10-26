"""Explicit factories for the validation Harness and acceptance policy."""

from aer_core.config.models import ValidationSettings
from aer_core.contracts.storage import ArtifactStore, VersionStore
from aer_core.contracts.validation import ValidationCache, ValidationTaskExecutor
from aer_core.plugin_registry import API_VERSION, PluginRegistry
from aer_core.schemas.plugins import PluginCapabilities, PluginMetadata
from .acceptance import ImprovementWithRegressionGuard, WeightedImprovement
from .paired import PairedValidation


def build_harness(*, config, dependencies):
    return PairedValidation(dependencies.get("task_executor"), dependencies.get("artifact_store"),
        dependencies.get("version_store"), dependencies.get("validation_cache"), dependencies.get("conditions"))


def build_policy(*, config, dependencies):
    return ImprovementWithRegressionGuard(config)


def build_weighted_policy(*, config, dependencies):
    return WeightedImprovement(config)


def register_validation_plugins(registry: PluginRegistry):
    registry.register(PluginMetadata(id="paired_validation", category="validation", api_version=API_VERSION,
        plugin_version="0.1.0", capabilities=PluginCapabilities(),
        dependencies=["task_executor", "artifact_store", "version_store", "validation_cache", "conditions"]),
        build_harness, config_model=ValidationSettings,
        dependency_types={"task_executor": ValidationTaskExecutor, "artifact_store": ArtifactStore,
                          "version_store": VersionStore, "validation_cache": ValidationCache, "conditions": dict})
    registry.register(PluginMetadata(id="improvement_with_regression_guard", category="acceptance_policy",
        api_version=API_VERSION, plugin_version="0.1.0", capabilities=PluginCapabilities(policy_versions=["v0.1"])),
        build_policy, config_model=ValidationSettings)
    registry.register(PluginMetadata(id="weighted_improvement", category="acceptance_policy",
        api_version=API_VERSION, plugin_version="0.1.0", capabilities=PluginCapabilities(policy_versions=["v0.1"])),
        build_weighted_policy, config_model=ValidationSettings)
