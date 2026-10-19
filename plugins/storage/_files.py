"""Shared run-directory locking and atomic file publication."""

import json
import os
import tempfile
import threading
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path

from aer_core.errors import StorageError


_LOCKS: dict[str, tuple[threading.RLock, threading.local]] = {}
_LOCKS_GUARD = threading.Lock()


def canonical_bytes(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def readable_json(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def key_path(identifier: str) -> str:
    return sha256(identifier.encode("utf-8")).hexdigest()


def unique_json(data: bytes) -> dict:
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def constant(value):
        raise ValueError(f"non-finite JSON value: {value}")

    return json.loads(data, object_pairs_hook=pairs, parse_constant=constant)


class FileRoot:
    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise StorageError(f"cannot open storage root {self.root}: {error}", stage="setup") from error
        with _LOCKS_GUARD:
            self._lock, self._state = _LOCKS.setdefault(os.path.normcase(str(self.root)), (threading.RLock(), threading.local()))

    def path(self, relative: str) -> Path:
        index = self.root / "storage_index.json"
        if relative != "storage_index.json" and index.exists():
            try:
                relative = unique_json(index.read_bytes()).get("aliases", {}).get(relative, relative)
            except (ValueError, UnicodeError, OSError) as error:
                raise StorageError("cannot read storage layout index", stage="setup") from error
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root):
            raise StorageError("storage reference escapes its root", stage="setup")
        return path

    @contextmanager
    def locked(self):
        with self._lock:
            if getattr(self._state, "depth", 0):
                self._state.depth += 1
                try:
                    yield
                finally:
                    self._state.depth -= 1
                return
            stream = None
            acquired = False
            try:
                stream = self.path(".storage.lock").open("a+b")
                if os.fstat(stream.fileno()).st_size == 0:
                    stream.write(b"\0")
                    stream.flush()
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError as error:
                if stream is not None:
                    stream.close()
                raise StorageError(f"cannot acquire storage lock for {self.root}: {error}", stage="setup") from error
            self._state.depth = 1
            try:
                yield
            finally:
                self._state.depth = 0
                try:
                    if acquired:
                        stream.seek(0)
                        if os.name == "nt":
                            import msvcrt
                            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                        else:
                            import fcntl
                            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                finally:
                    stream.close()

    def read(self, relative: str) -> bytes:
        try:
            return self.path(relative).read_bytes()
        except OSError as error:
            raise StorageError(f"cannot read {relative}: {error}", stage="setup") from error

    def read_json(self, relative: str) -> dict:
        try:
            return unique_json(self.read(relative))
        except (ValueError, UnicodeError) as error:
            raise StorageError(f"invalid JSON record {relative}: {error}", stage="setup") from error

    def immutable(self, relative: str, content: bytes) -> None:
        with self.locked():
            path = self.path(relative)
            if path.exists():
                stored = self.read(relative)
                # Older runs use compact JSON. Whitespace is not a domain change.
                equivalent = stored == content
                if not equivalent and path.suffix == ".json":
                    try:
                        equivalent = canonical_bytes(unique_json(stored)) == canonical_bytes(unique_json(content))
                    except (ValueError, UnicodeError) as error:
                        raise StorageError(f"invalid immutable JSON record: {relative}", stage="setup") from error
                if not equivalent:
                    raise StorageError(f"immutable record already exists with different content: {relative}", stage="setup")
                return
            self.replace(relative, content)

    def replace(self, relative: str, content: bytes) -> None:
        """Caller holds the run lock. Only mutable pointers use replacement."""
        path = self.path(relative)
        if path.suffix == ".json":
            content = readable_json(unique_json(content))
        temporary = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".tmp-", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            temporary = None
        except OSError as error:
            raise StorageError(f"cannot publish {relative}: {error}", stage="setup") from error
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
