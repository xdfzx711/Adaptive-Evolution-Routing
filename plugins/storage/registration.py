"""Explicit local storage registrations, using the same plugin API as mocks."""

from aer_core.config.models import NonBlank, Settings
from aer_core.contracts.storage import ArtifactStore, ExperienceStore
from aer_core.plugin_registry import API_VERSION, PluginRegistry, ResolvedDependencies
from aer_core.schemas.plugins import PluginMetadata

from .artifacts import LocalArtifactStore
from .experience import JsonlExperienceStore
from .versions import LocalVersionStore


class LocalStoreConfig(Settings):
    root: NonBlank


class LocalVersionConfig(LocalStoreConfig):
    scope_id: NonBlank = "default"


def build_artifacts(*, config: LocalStoreConfig, dependencies: ResolvedDependencies) -> LocalArtifactStore:
    return LocalArtifactStore(config.root)


def build_experience(*, config: LocalStoreConfig, dependencies: ResolvedDependencies) -> JsonlExperienceStore:
    return JsonlExperienceStore(config.root)


def build_versions(*, config: LocalVersionConfig, dependencies: ResolvedDependencies) -> LocalVersionStore:
    return LocalVersionStore(config.root, dependencies.get("artifact_store"), dependencies.get("experience_store"), scope_id=config.scope_id)


def register_local_storage(registry: PluginRegistry) -> None:
    registry.register(PluginMetadata(id="local_files", category="artifact_store", api_version=API_VERSION, plugin_version="0.1.0"), build_artifacts, config_model=LocalStoreConfig)
    registry.register(PluginMetadata(id="jsonl", category="experience_store", api_version=API_VERSION, plugin_version="0.1.0"), build_experience, config_model=LocalStoreConfig)
    registry.register(PluginMetadata(id="local_manifest", category="version_store", api_version=API_VERSION, plugin_version="0.1.0", dependencies=["artifact_store", "experience_store"]), build_versions, config_model=LocalVersionConfig, dependency_types={"artifact_store": ArtifactStore, "experience_store": ExperienceStore})
