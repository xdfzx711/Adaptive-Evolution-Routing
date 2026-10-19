"""Offline migration to round directories, preserving immutable content bytes."""

import json
import zipfile
from datetime import datetime
from pathlib import Path

from aer_core.errors import StorageError
from aer_core.identifiers import timestamp_id
from aer_core.schemas.references import ArtifactRef
from plugins.storage._files import FileRoot, canonical_bytes, key_path, unique_json
from plugins.storage.artifacts import LocalArtifactStore
from plugins.storage.experience import JsonlExperienceStore
from plugins.storage.layout import RunLayout, compact_plugins, episode_directory, safe_name
from plugins.storage.versions import LocalVersionStore


def reorganize_run(root):
    root = Path(root).expanduser().resolve()
    if not root.is_dir() or not (root / "storage_manifest.json").is_file():
        if (root / "storage_index.json").is_file() and (root / "run_manifest.json").is_file():
            return {"status": "already_organized", "run_root": str(root)}
        raise StorageError("migration requires a saved legacy run directory", stage="setup")
    files = FileRoot(root)
    with files.locked():
        return _migrate(root, files)


def _migrate(root, files):
    original_paths = [path for path in root.rglob("*") if path.is_file()]
    for path in original_paths:
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise StorageError("migration source must stay inside the run directory", stage="setup")
    storage = files.read_json("storage_manifest.json")
    events = JsonlExperienceStore(root).events()
    finished = {event.episode_id for event in events if event.record_type == "task_finished"}
    if any(event.episode_id not in finished for event in events):
        raise StorageError("finish the task before reorganizing its run directory", stage="setup")
    episodes = [files.read_json(path.relative_to(root).as_posix()) for path in (root / "episodes").glob("*.json")]
    directories = {item["run_context"]["episode_id"]: episode_directory(item["task"]["task_id"], item["run_context"]["episode_id"])
                   for item in episodes}
    attempts = {event.payload.attempt_id: event.payload for event in events if event.record_type == "attempt_started"}
    attempt_dirs = {identity: f"{directories[item.episode_id]}/round-{item.round_id:03d}" for identity, item in attempts.items()}
    candidate_dirs = {}
    for event in events:
        if event.record_type == "attempt_finished":
            for candidate in event.payload.candidate_ids:
                candidate_dirs[candidate] = attempt_dirs[event.payload.attempt_id]

    artifact_index = files.read_json("artifacts/index.json") if files.path("artifacts/index.json").exists() else {}
    # Legacy hash runs may not have the newer artifact index. Recover references
    # from their typed metadata before moving anything.
    def collect_refs(value):
        if isinstance(value, dict):
            if {"artifact_id", "kind", "version", "uri", "digest"} <= value.keys():
                ref = ArtifactRef.model_validate(value)
                artifact_index.setdefault(ref.artifact_id, ref.model_dump(mode="json"))
            else:
                for item in value.values():
                    collect_refs(item)
        elif isinstance(value, list):
            for item in value:
                collect_refs(item)
    for path in original_paths:
        if path.suffix in {".json", ".bin"}:
            try:
                collect_refs(unique_json(path.read_bytes()))
            except (ValueError, UnicodeError):
                pass
    contents = {}
    artifact_dirs = {}
    def assign(ref, directory):
        if ref:
            identity = ref.artifact_id if hasattr(ref, "artifact_id") else ref["artifact_id"]
            artifact_dirs.setdefault(identity, directory)
    for attempt in attempts.values():
        for ref in (attempt.pre_state_ref, attempt.trajectory_ref):
            assign(ref, attempt_dirs[attempt.attempt_id])
    for identity, ref in artifact_index.items():
        raw = files.read(ref["uri"])
        LocalArtifactStore(root).get(ArtifactRef.model_validate(ref))
        try:
            data = unique_json(raw)
        except (ValueError, UnicodeError):
            continue
        if not isinstance(data, dict):
            continue
        contents[identity] = data
        kind = ref["kind"]
        if kind == "evolution_proposal":
            directory = f"{directories[data['episode_id']]}/round-{data['round_id']:03d}"
            artifact_dirs[identity] = directory
            for candidate in (data.get("response") or {}).get("candidates", []):
                for update in candidate["artifact_updates"].values():
                    assign(update, directory)
        elif kind == "validation_batch":
            artifact_dirs[identity] = attempt_dirs[data["attempt_id"]]
        elif kind == "run_result":
            directory = directories[data["episode_id"]]
            artifact_dirs[identity] = f"{directory}/round-{data['evolution_rounds_used']:03d}" if data["evolution_rounds_used"] else directory + "/initial"
        elif kind == "diagnosis":
            following = sorted((item for item in attempts.values() if item.episode_id == data["episode_id"]
                and item.started_at.isoformat() >= data["finished_at"].replace("Z", "+00:00")), key=lambda item: item.round_id)
            number = following[0].round_id if following else 1
            artifact_dirs[identity] = f"{directories[data['episode_id']]}/round-{number:03d}"
        elif kind == "task_execution":
            directory = directories.get(data["episode_id"], episode_directory(data["task"]["task_id"], data["episode_id"]))
            artifact_dirs[identity] = directory + "/initial"

    reports = [files.read_json(path.relative_to(root).as_posix())["report"] for path in (root / "validation").glob("*.json")]
    baseline_dirs = {}
    version_dirs = {}
    for report in sorted(reports, key=lambda value: value["validation_id"]):
        directory = candidate_dirs[report["candidate_id"]]
        baseline_dirs.setdefault(report["baseline_version_id"], directory)
        version_dirs[report["candidate_version_id"]] = directory
        for key in ("local_result_ref", "private_results_ref"):
            assign(report[key], directory)
    for identity, data in contents.items():
        if artifact_index[identity]["kind"] != "controller_execution":
            continue
        execution = data.get("execution") or {}
        operation = execution.get("operation_id") or (data.get("error") or {}).get("operation_id", "")
        episode = next((key for key in directories if operation.startswith(key + "/")), None)
        if episode:
            artifact_dirs[identity] = (version_dirs.get(data["version_id"], directories[episode] + "/initial")
                                       if "/post_commit_execution-" in operation else directories[episode] + "/initial")

    index = {"schema_version": "aer.layout.v1", "artifacts": artifact_index, "records": {}, "aliases": {}}
    moves = {}
    rewrites = {}
    def plan(source, destination, category=None, identity=None):
        source_path, destination_path = root / source, root / destination
        for path in (source_path, destination_path):
            if not path.resolve().is_relative_to(root):
                raise StorageError("migration target escapes the run directory", stage="setup")
        if source != destination:
            if destination_path.exists() or destination in moves.values():
                raise StorageError(f"migration destination already exists: {destination}", stage="setup")
            moves[source] = destination
            index["aliases"][source] = destination
        if category:
            index["records"].setdefault(category, {})[identity] = destination
        return destination

    for identity, ref in artifact_index.items():
        directory = artifact_dirs.get(identity, "rounds/initial")
        filename = Path(ref["uri"]).name
        if filename.startswith("sha256-"):
            filename = timestamp_id(LocalArtifactStore._prefix(ref["kind"])) + ".bin"
        plan(ref["uri"], f"{directory}/artifacts/{filename}")
    for path in (root / "versions/manifests").glob("*.json"):
        data = files.read_json(path.relative_to(root).as_posix())
        snapshot = data["snapshot"]
        directory = version_dirs.get(snapshot["version_id"], "rounds/initial")
        plan(path.relative_to(root).as_posix(), f"{directory}/versions/{timestamp_id('snapshot')}.json", "snapshots", snapshot["version_id"])
    for path in (root / "versions/candidates").glob("*.json"):
        data = files.read_json(path.relative_to(root).as_posix())
        identity = data["candidate_id"]
        directory = candidate_dirs[identity]
        plan(path.relative_to(root).as_posix(), f"{directory}/versions/{timestamp_id('candidate-' + identity.rsplit('/',1)[-1])}.json", "candidates", identity)
    for report in reports:
        source = "validation/" + report["validation_id"] + ".json"
        if not (root / source).exists():
            source = "validation/" + key_path(report["validation_id"]) + ".json"
        directory = candidate_dirs[report["candidate_id"]]
        plan(source, f"{directory}/validation/{report['validation_id']}.json", "validations", report["validation_id"])
    for path in (root / "validation/batches").glob("*.json"):
        data = files.read_json(path.relative_to(root).as_posix())
        directory = attempt_dirs[data["attempt_id"]]
        plan(path.relative_to(root).as_posix(), f"{directory}/validation/{timestamp_id('batch')}.json", "batches", data["attempt_id"])
    for path in (root / "validation/cache").glob("*.json"):
        data = files.read_json(path.relative_to(root).as_posix())
        result = data["result"]
        directory = baseline_dirs.get(result["version_id"], "rounds/initial")
        plan(path.relative_to(root).as_posix(), f"{directory}/validation/cache/{timestamp_id(safe_name(result['task']['task_id']))}.json", "cache", data["key"])
    scope_names = {key_path("default"): ("default", "rounds/initial"),
                   key_path("orchestrator/continual"): ("orchestrator/continual", "rounds/continual")}
    for episode in episodes:
        identity = episode["run_context"]["episode_id"]
        scope_names[key_path(episode["scope_id"])] = (episode["scope_id"],
            "rounds/continual" if episode["task_mode"] == "continual" else directories[identity])
        source = "episodes/" + key_path(identity) + ".json"
        target = plan(source, "episodes/" + safe_name(identity) + ".json", "episodes", identity)
        episode["version_store"] = {"plugin": storage["experiment_config"]["storage"]["versions"]}
        rewrites[target] = canonical_bytes(episode)
    for path in (root / "versions/scopes").glob("*/stable.json"):
        scope, directory = scope_names[path.parent.name]
        plan(path.relative_to(root).as_posix(), directory + "/stable.json", "scopes", scope)
    for group, category in (("intents", "intents"), ("committed", "receipts")):
        for path in (root / "versions/scopes").glob(f"*/{group}/*.json"):
            data = files.read_json(path.relative_to(root).as_posix())
            directory = attempt_dirs[data["record"]["attempt_id"]]
            plan(path.relative_to(root).as_posix(), f"{directory}/versions/{timestamp_id('commit-intent' if group == 'intents' else 'commit-receipt')}.json", category, path.stem)

    manifest = {"schema_version": "aer.run.v1"}
    storage["plugins"] = compact_plugins(storage["plugins"])
    storage["initial_snapshot_ref"]["uri"] = index["records"]["snapshots"][storage["initial_snapshot_ref"]["record_id"]]
    manifest["storage"] = storage
    names = ["storage_manifest.json"]
    for name in ("execution", "evolution", "orchestrator", "validation"):
        source = root / f"{name}_manifest.json"
        if not source.exists():
            continue
        data = unique_json(source.read_bytes())
        names.append(source.name)
        if name == "evolution":
            data = {"plugins": compact_plugins(data)}
        elif name == "execution":
            data["plugins"] = compact_plugins(data["plugins"])
            for key in ("assembly_manifest", "notes", "source_path", "status", "index"):
                data["benchmark"].pop(key, None)
        elif name == "validation":
            data = {"plugins": compact_plugins(data["plugins"]), "budget_enforced": data["budget_enforced"],
                    "dataset_version": data["conditions"]["dataset_version"], "scorer_version": data["conditions"]["scorer_version"]}
        manifest[name] = data

    backup = root.parent / ".migration-backups" / (timestamp_id(root.name) + ".zip")
    backup.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(backup, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in original_paths:
            if path.name != ".storage.lock":
                archive.write(path, path.relative_to(root).as_posix())
    try:
        # Files move first; publish aliases only after their destinations exist.
        for source, destination in moves.items():
            target = root / destination
            target.parent.mkdir(parents=True, exist_ok=True)
            (root / source).rename(target)
        files.replace("storage_index.json", canonical_bytes(index))
        for relative, content in rewrites.items():
            files.replace(relative, content)
        files.replace("run_manifest.json", canonical_bytes(manifest))
        if (root / "evolution.txt").exists():
            text = (root / "evolution.txt").read_text(encoding="utf-8")
            for source, destination in moves.items():
                text = text.replace(str(root / source), str(root / destination))
            files.replace("evolution.txt", text.encode("utf-8"))
        # Validate every artifact and version, and recover/check stable receipts.
        artifacts = LocalArtifactStore(root)
        for ref in artifact_index.values():
            artifacts.get(ArtifactRef.model_validate(ref))
        versions = LocalVersionStore(root, artifacts)
        for identity in index["records"].get("snapshots", {}):
            versions.load(identity)
        for scope in index["records"].get("scopes", {}):
            LocalVersionStore(root, artifacts, scope_id=scope).stable()
        for name in names + ["artifacts/index.json"]:
            path = root / name
            if path.exists():
                path.unlink()
        for directory in sorted((path for path in root.rglob("*") if path.is_dir()), key=lambda path: len(path.parts), reverse=True):
            if not any(directory.iterdir()):
                directory.rmdir()
    except Exception:
        # Restore byte-for-byte originals, keeping the external backup available.
        for source, destination in reversed(list(moves.items())):
            target = root / destination
            if target.exists():
                original = root / source
                original.parent.mkdir(parents=True, exist_ok=True)
                target.replace(original)
        for relative in ("storage_index.json", "run_manifest.json"):
            path = root / relative
            if path.exists():
                path.unlink()
        with zipfile.ZipFile(backup) as archive:
            for name in archive.namelist():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(archive.read(name))
        raise
    return {"status": "organized", "run_root": str(root), "moved_files": len(moves), "backup": str(backup),
            "manifest": str(root / "run_manifest.json")}
