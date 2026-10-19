"""Prepare one persistent run directory without constructing execution plugins."""

import re
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from aer_core.identifiers import timestamp_id

from aer_core.config import LoadedConfig
from aer_core.contracts.storage import ArtifactStore, ExperienceStore, VersionStore
from aer_core.errors import StorageError
from aer_core.plugin_manager import PluginManager
from aer_core.schemas.plugins import PluginBinding
from aer_core.schemas.references import AgentSnapshot, RecordRef
from aer_core.schemas.storage import StorageManifest
from plugins.storage._files import FileRoot, canonical_bytes
from plugins.storage.artifacts import LocalArtifactStore
from plugins.storage.versions import LocalVersionStore
from plugins.storage.layout import RunLayout, compact_plugins, write_manifest

from .bootstrap import create_registry, controlled_snapshot_config, initial_artifact_refs
from .configuration import load_experiment_config
from .environment import resolve_model


@dataclass(frozen=True)
class PreparedStorage:
    loaded_config: LoadedConfig
    run_id: str
    run_root: Path
    artifact_store: ArtifactStore
    version_store: VersionStore
    experience_store: ExperienceStore
    initial_snapshot_ref: RecordRef
    manifest: StorageManifest

    def as_dict(self):
        return {
            "status": "storage_initialized", "run_id": self.run_id,
            "run_root": str(self.run_root), "manifest_path": str(self.run_root / "run_manifest.json"),
            "initial_snapshot_ref": self.initial_snapshot_ref.model_dump(mode="json"),
            "stable_version_id": self.version_store.stable().version_id,
            "notes": ["Storage initialized without task execution, model calls or evolution."],
        }


def _import_snapshot(path: str, artifacts: ArtifactStore, versions: VersionStore) -> AgentSnapshot:
    source = Path(path).resolve()
    source_root = next((parent for parent in source.parents if
        (parent / "storage_index.json").exists() or (parent / "storage_manifest.json").exists()), None)
    if source_root is None:
        raise StorageError("initial_snapshot_ref must refer to a saved local Snapshot manifest", stage="setup")
    old_artifacts = LocalArtifactStore(source_root)
    old_versions = LocalVersionStore(source_root, old_artifacts)
    from aer_core.schemas.storage import SnapshotManifest
    try:
        manifest = SnapshotManifest.model_validate(FileRoot(source_root).read_json(source.relative_to(source_root).as_posix()))
    except ValueError as error:
        raise StorageError(f"invalid initial Snapshot manifest: {error}", stage="setup") from error
    if old_versions._snapshot_path(manifest.snapshot.version_id) != source.relative_to(source_root).as_posix():
        raise StorageError("Snapshot file name does not match its version identity", stage="setup")
    chain = []
    seen = set()
    snapshot = old_versions.load(manifest.snapshot.version_id)
    while True:
        if snapshot.version_id in seen:
            raise StorageError("Snapshot lineage contains a cycle", stage="setup")
        seen.add(snapshot.version_id)
        chain.append(snapshot)
        if snapshot.parent_version_id is None:
            break
        snapshot = old_versions.load(snapshot.parent_version_id)
    for snapshot in reversed(chain):
        for ref in snapshot.artifacts.values():
            content = old_artifacts.get(ref)
            imported = artifacts.import_ref(ref, content) if isinstance(artifacts, LocalArtifactStore) else artifacts.put(content, ref.kind)
            if imported != ref:
                raise StorageError("imported artifact differs from frozen Snapshot reference", stage="setup")
        versions.save(snapshot)
    return chain[0]


def prepare_storage(
    path: str | Path, *, run_id: str | None = None, benchmark_index: str | Path | None = None,
    loaded_config: LoadedConfig | None = None,
) -> PreparedStorage:
    loaded = resolve_model(loaded_config or load_experiment_config(path, benchmark_index=benchmark_index), required=True)
    config = loaded.config
    if run_id is None:
        run_id = timestamp_id("run")
        while (Path(config.storage.root) / run_id).exists():
            run_id = timestamp_id("run")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", run_id):
        raise StorageError("run_id must be a single ASCII directory name of 1-128 characters", stage="setup")
    root = Path(config.storage.root) / run_id
    layout = RunLayout(root)
    if not (root / "storage_manifest.json").exists():
        layout.initialize()
    registry = create_registry()
    built = PluginManager(registry).build([
        PluginBinding(name="artifacts", category="artifact_store", plugin_id=config.storage.artifacts, config={"root": str(root)}),
        PluginBinding(name="experience", category="experience_store", plugin_id=config.storage.experience, config={"root": str(root)}),
        PluginBinding(name="versions", category="version_store", plugin_id=config.storage.versions, config={"root": str(root), "scope_id": "default"}, dependency_bindings={"artifact_store": "artifacts", "experience_store": "experience"}),
    ])
    artifacts, versions, experience = (built.instances[name] for name in ("artifacts", "versions", "experience"))
    if config.agent.initial_snapshot_ref is not None:
        initial = _import_snapshot(config.agent.initial_snapshot_ref, artifacts, versions)
        expected = controlled_snapshot_config(config, registry)
        if initial.agent_plugin_id != config.agent.plugin or initial.model_dump()["config"] != expected:
            raise StorageError("frozen Snapshot Agent/model/tool configuration does not match this experiment", stage="setup")
    else:
        components = initial_artifact_refs(config, registry, artifacts)
        controlled = controlled_snapshot_config(config, registry)
        # Content identity must not depend on the timestamp of a physical file.
        canonical_components = {kind: {**ref.model_dump(mode="json"), "uri": f"artifacts/{ref.artifact_id}.bin"}
                                for kind, ref in components.items()}
        identity = {"agent": config.agent.plugin, "config": controlled, "artifacts": canonical_components}
        initial = AgentSnapshot(version_id="initial-" + sha256(canonical_bytes(identity)).hexdigest(), agent_plugin_id=config.agent.plugin, config=controlled, artifacts=components)
    ref = versions.initialize(initial)
    manifest = StorageManifest(run_id=run_id, experiment_id=config.experiment.id, source_config=str(loaded.source_path), experiment_config=config.model_dump(mode="json"), plugins=built.manifest, initial_snapshot_ref=ref)
    saved = manifest.model_dump(mode="json")
    saved["plugins"] = compact_plugins(built.manifest)
    write_manifest(root, "storage", saved)
    return PreparedStorage(loaded, run_id, root, artifacts, versions, experience, ref, manifest)
