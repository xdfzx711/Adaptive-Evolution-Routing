"""Physical round layout; logical content/version identities stay independent."""

import re
from contextvars import ContextVar

from aer_core.identifiers import timestamp_id
from ._files import FileRoot, canonical_bytes, key_path


_PLACEMENT = ContextVar("aer_storage_placement", default={})
STAMP = r"\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_\d{6}"


def safe_name(value):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value)[:100].strip(".-") or "record"


def episode_directory(task_id, episode_id):
    stamp = re.search(STAMP + r"$", episode_id)
    return f"rounds/{safe_name(task_id)}-{stamp.group() if stamp else safe_name(episode_id)}"


class RunLayout:
    index_path = "storage_index.json"

    def __init__(self, root):
        self.files = FileRoot(root)
        self.root_key = str(self.files.root)

    @property
    def enabled(self):
        return (self.files.root / self.index_path).exists()

    def initialize(self):
        _PLACEMENT.set({key: value for key, value in _PLACEMENT.get().items() if key != self.root_key})
        with self.files.locked():
            if not self.enabled:
                self.files.immutable(self.index_path, canonical_bytes({
                    "schema_version": "aer.layout.v1", "artifacts": {}, "records": {}, "aliases": {}}))

    def read(self):
        return self.files.read_json(self.index_path)

    def update(self, change):
        with self.files.locked():
            data = self.read()
            change(data)
            self.files.replace(self.index_path, canonical_bytes(data))

    @property
    def directory(self):
        return _PLACEMENT.get().get(self.root_key, {}).get("directory", "rounds/initial")

    def begin_episode(self, task, context):
        self.select(task.task_id, context.episode_id)

    def begin_round(self, number):
        state = _PLACEMENT.get().get(self.root_key, {})
        self._place(state.get("episode_directory", "rounds/initial"), f"round-{number:03d}")

    def select(self, task_id, episode_id, round_id=None):
        self._place(episode_directory(task_id, episode_id), f"round-{round_id:03d}" if round_id else "initial")

    def _place(self, episode, phase):
        _PLACEMENT.set({**_PLACEMENT.get(), self.root_key: {
            "episode_directory": episode, "directory": f"{episode}/{phase}"}})

    def named(self, category, identity, legacy, *, folder, prefix, directory=None, filename=None):
        if not self.enabled:
            return legacy
        with self.files.locked():
            data = self.read()
            entries = data["records"].setdefault(category, {})
            if identity in entries:
                return entries[identity]
            base = directory or self.directory
            name = filename or ((prefix if re.fullmatch(STAMP, prefix.removeprefix("validation-")) else timestamp_id(safe_name(prefix))) + ".json")
            relative = f"{base}/{folder}/{name}" if folder else f"{base}/{name}"
            if relative in entries.values():
                relative = f"{base}/{folder}/{timestamp_id(safe_name(prefix))}.json"
            entries[identity] = relative
            self.files.replace(self.index_path, canonical_bytes(data))
            return relative

    def episode_path(self, episode_id):
        return self._episode_path(episode_id) if self.enabled else f"episodes/{key_path(episode_id)}.json"

    def _episode_path(self, episode_id):
        with self.files.locked():
            data = self.read()
            entries = data["records"].setdefault("episodes", {})
            if episode_id not in entries:
                entries[episode_id] = f"episodes/{safe_name(episode_id)}.json"
                self.files.replace(self.index_path, canonical_bytes(data))
            return entries[episode_id]

    def scope_path(self, scope_id):
        if not self.enabled:
            return f"versions/scopes/{key_path(scope_id)}/stable.json"
        state = _PLACEMENT.get().get(self.root_key, {})
        directory = ("rounds/initial" if scope_id == "default" else "rounds/continual" if scope_id == "orchestrator/continual"
                     else state.get("episode_directory", "rounds/" + safe_name(scope_id)))
        return self.named("scopes", scope_id, "", folder="", prefix="stable", directory=directory, filename="stable.json")

    def artifact_index(self):
        return self.read()["artifacts"]

    def write_artifact_index(self, index):
        self.update(lambda data: data.update(artifacts=index))

    def alias(self, source, destination):
        self.files.path(destination)
        self.update(lambda data: data["aliases"].update({source: destination}))


def compact_plugins(manifest):
    data = manifest.model_dump(mode="json") if hasattr(manifest, "model_dump") else manifest
    return [{"binding": item["binding_name"], **{key: item["descriptor"]["metadata"][key]
             for key in ("id", "category", "api_version", "plugin_version")}}
            for item in data.get("instances", [])]


def write_manifest(root, section, value):
    """Keep only one run manifest, with each frozen startup section written once."""
    files = FileRoot(root)
    with files.locked():
        data = files.read_json("run_manifest.json") if files.path("run_manifest.json").exists() else {"schema_version": "aer.run.v1"}
        if section in data and canonical_bytes(data[section]) != canonical_bytes(value):
            from aer_core.errors import StorageError
            raise StorageError(f"run manifest section changed: {section}", stage="setup")
        data[section] = value
        files.replace("run_manifest.json", canonical_bytes(data))


def read_manifest(root, section, legacy_name):
    files = FileRoot(root)
    if files.path("run_manifest.json").exists():
        return files.read_json("run_manifest.json")[section]
    return files.read_json(legacy_name)
