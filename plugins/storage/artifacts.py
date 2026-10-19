"""Immutable, content-addressed local bytes with portable relative references."""

import re
from hashlib import sha256
from pathlib import Path
from pydantic import ValidationError

from aer_core.errors import StorageError
from aer_core.identifiers import timestamp_id
from aer_core.schemas.references import ArtifactRef

from ._files import FileRoot, canonical_bytes
from .layout import RunLayout


class LocalArtifactStore:
    index_path = "artifacts/index.json"

    def __init__(self, root: str | Path):
        self.files = FileRoot(root)
        self.layout = RunLayout(root)

    def _ref(self, content: bytes, kind: str) -> ArtifactRef:
        digest = sha256(content).hexdigest()
        identity = "sha256-" + sha256(kind.encode("utf-8") + b"\0" + content).hexdigest()
        return ArtifactRef(artifact_id=identity, kind=kind, version=digest, uri=f"artifacts/{identity}.bin", digest=digest)

    def put(self, content: bytes, kind: str) -> ArtifactRef:
        if not isinstance(content, bytes):
            raise StorageError("artifact content must be bytes", stage="setup")
        if not isinstance(kind, str) or not kind.strip():
            raise StorageError("artifact kind must be a non-empty string", stage="setup")
        identity = self._ref(content, kind)
        with self.files.locked():
            index = self._index()
            if identity.artifact_id in index:
                try:
                    ref = ArtifactRef.model_validate(index[identity.artifact_id])
                except ValidationError as error:
                    raise StorageError("invalid artifact index entry", stage="setup") from error
                if self.get(ref) != content or ref.kind != kind:
                    raise StorageError("artifact index differs from immutable content", stage="setup")
                return ref
            # Reopening a pre-timestamp run must not relocate existing artifacts.
            if self.files.path(identity.uri).exists():
                self.get(identity)
                return identity
            prefix = self._prefix(kind)
            directory = self.layout.directory + "/artifacts" if self.layout.enabled else "artifacts"
            ref = identity.replace(uri=directory + "/" + timestamp_id(prefix) + ".bin")
            while self.files.path(ref.uri).exists():
                ref = ref.replace(uri=directory + "/" + timestamp_id(prefix) + ".bin")
            self.files.immutable(ref.uri, content)
            index[ref.artifact_id] = ref.model_dump(mode="json")
            self._write_index(index)
            return ref

    def _index(self):
        if self.layout.enabled:
            return self.layout.artifact_index()
        index = self.files.read_json(self.index_path) if self.files.path(self.index_path).exists() else {}
        if not isinstance(index, dict):
            raise StorageError("artifact index must be a mapping", stage="setup")
        return index

    def _write_index(self, index):
        if self.layout.enabled:
            self.layout.write_artifact_index(index)
        else:
            self.files.replace(self.index_path, canonical_bytes(index))

    def import_ref(self, ref: ArtifactRef, content: bytes) -> ArtifactRef:
        """Preserve portable frozen Snapshot references when importing another run."""
        expected = self._ref(content, ref.kind).replace(uri=ref.uri)
        if expected != ref or not self._valid_uri(ref):
            raise StorageError("imported artifact differs from its frozen reference", stage="setup")
        with self.files.locked():
            if self.layout.enabled and ref.uri.startswith("artifacts/"):
                destination = self.layout.directory + "/artifacts/" + timestamp_id(self._prefix(ref.kind)) + ".bin"
                if not self.files.path(ref.uri).exists():
                    self.layout.alias(ref.uri, destination)
            self.files.immutable(ref.uri, content)
            index = self._index()
            index.setdefault(ref.artifact_id, ref.model_dump(mode="json"))
            self._write_index(index)
        return ref

    @staticmethod
    def _prefix(kind):
        return re.sub(r"[^A-Za-z0-9_-]", "_", kind)[:48].strip("_-") or "artifact"

    @staticmethod
    def _valid_uri(ref):
        return ref.uri == f"artifacts/{ref.artifact_id}.bin" or re.fullmatch(
            r"(?:artifacts|rounds/(?:[A-Za-z0-9_.-]+/)+artifacts)/" + re.escape(LocalArtifactStore._prefix(ref.kind)) +
            r"-\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_\d{6}\.bin", ref.uri) is not None

    def get(self, ref: ArtifactRef) -> bytes:
        if not re.fullmatch(r"sha256-[0-9a-f]{64}", ref.artifact_id) or not self._valid_uri(ref):
            raise StorageError("artifact reference does not belong to the local format", stage="setup")
        with self.files.locked():
            content = self.files.read(ref.uri)
            if self._ref(content, ref.kind).replace(uri=ref.uri) != ref:
                raise StorageError(f"artifact content or identity differs from its reference: {ref.artifact_id}", stage="setup")
            return content
