"""Readable local-time identities; content-addressed artifacts retain their hashes."""

from datetime import datetime, timedelta, timezone
from threading import Lock

_lock = Lock()
_last = datetime.min.replace(tzinfo=timezone.utc)


def timestamp_id(prefix: str) -> str:
    global _last
    with _lock:
        now = max(datetime.now(timezone.utc), _last + timedelta(microseconds=1))
        _last = now
    return prefix + "-" + now.astimezone().strftime("%Y-%m-%d_%H-%M-%S_%f")
