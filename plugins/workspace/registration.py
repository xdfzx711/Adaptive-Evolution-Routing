from pathlib import Path

from aer_core.config.models import Settings
from aer_core.contracts.storage import ArtifactStore
from aer_core.plugin_registry import API_VERSION
from aer_core.schemas.plugins import PluginMetadata
from .local import EmptyWorkspace, LocalRepoWorkspace, LocalRepoSettings
from .collectors import JsonResultCollector, GitPatchCollector, PatchCollectorSettings


class EmptySettings(Settings):
    pass


def build_empty(*, config, dependencies):
    return EmptyWorkspace()


def build_repo(*, config, dependencies):
    return LocalRepoWorkspace(config, dependencies.get("config_root"))


def build_json(*, config, dependencies):
    return JsonResultCollector()


def build_patch(*, config, dependencies):
    return GitPatchCollector(config, dependencies.get("artifact_store"))


def register_workspace_plugins(registry):
    for name, category, factory, settings, deps in (
        ("empty_workspace", "workspace_provider", build_empty, EmptySettings, {}),
        ("local_repo_workspace", "workspace_provider", build_repo, LocalRepoSettings, {"config_root": Path}),
        ("json_result_collector", "result_collector", build_json, EmptySettings, {}),
        ("git_patch_collector", "result_collector", build_patch, PatchCollectorSettings, {"artifact_store": ArtifactStore}),
    ):
        registry.register(PluginMetadata(id=name, category=category, api_version=API_VERSION,
            plugin_version="0.1.1" if name == "local_repo_workspace" else "0.1.0", dependencies=tuple(deps)), factory,
            config_model=settings, dependency_types=deps)
