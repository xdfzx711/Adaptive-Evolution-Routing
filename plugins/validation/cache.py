"""Run-local, immutable cache of complete task execution and scoring evidence."""

from aer_core.errors import StorageError
from aer_core.schemas.paired import ScoredExecution
from plugins.storage._files import FileRoot, canonical_bytes, key_path
from plugins.storage.layout import RunLayout


class LocalValidationCache:
    def __init__(self, root):
        self.files = FileRoot(root)
        self.layout = RunLayout(root)

    def _path(self, key, task_id="baseline"):
        return self.layout.named("cache", key, "validation/cache/" + key_path(key) + ".json",
                                 folder="validation/cache", prefix=task_id)

    def get(self, key: str) -> ScoredExecution | None:
        if self.layout.enabled and key not in self.layout.read()["records"].get("cache", {}):
            return None
        relative = self._path(key)
        with self.files.locked():
            if not self.files.path(relative).exists():
                return None
            try:
                payload = self.files.read_json(relative)
                if payload["key"] != key:
                    raise ValueError("cache identity differs")
                result = ScoredExecution.model_validate(payload["result"])
                if not result.complete:
                    raise ValueError("cache entry is incomplete")
                return result
            except (ValueError, KeyError) as error:
                raise StorageError("Invalid task execution cache", stage="setup") from error

    def put(self, key: str, result: ScoredExecution) -> None:
        if not result.complete:
            return
        relative = self._path(key, result.task.task_id)
        with self.files.locked():
            if not self.files.path(relative).exists():
                self.files.immutable(relative, canonical_bytes({"key": key, "result": result.model_dump(mode="json")}))
