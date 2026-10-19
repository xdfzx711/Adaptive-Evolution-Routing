"""Idempotent typed JSONL events, audit access and time-bounded history queries."""

import os
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from aer_core.errors import StorageError
from aer_core.schemas.experience import (
    AttemptFinishedEvent, AttemptStartedEvent, CandidateEvaluatedEvent,
    ExperienceEvent, ExperienceQuery, TaskFinishedEvent, VersionCommittedEvent,
)
from aer_core.schemas.references import RecordRef

from ._files import FileRoot, canonical_bytes, unique_json


_EVENT = TypeAdapter(ExperienceEvent)


class JsonlExperienceStore:
    filename = "experience/events.jsonl"

    def __init__(self, root: str | Path):
        self.files = FileRoot(root)

    def _read(self):
        path = self.files.path(self.filename)
        if not path.exists():
            return (), 0, False
        raw = self.files.read(self.filename)
        events, ids = [], {}
        offset = 0
        for line in raw.splitlines(keepends=True):
            terminated = line.endswith(b"\n")
            try:
                event = _EVENT.validate_python(unique_json(line))
                content = canonical_bytes(_EVENT.dump_python(event, mode="json"))
            except (ValueError, UnicodeError, ValidationError) as error:
                if not terminated:
                    return tuple(events), offset, False
                raise StorageError(f"invalid JSONL record at byte {offset}: {error}", stage="setup") from error
            if event.event_id in ids:
                raise StorageError(f"duplicate stored event_id: {event.event_id}", stage="setup")
            ids[event.event_id] = content
            events.append(event)
            offset += len(line)
        return tuple(events), offset, bool(raw and not raw.endswith(b"\n"))

    def events(self) -> tuple[ExperienceEvent, ...]:
        """Full readable audit log, including unfinished attempts."""
        with self.files.locked():
            return self._read()[0]

    def get(self, ref: RecordRef) -> ExperienceEvent:
        if ref.uri != self.filename + "#" + ref.record_id:
            raise StorageError("experience reference does not belong to the local format", stage="setup")
        with self.files.locked():
            for event in self._read()[0]:
                if event.event_id == ref.record_id:
                    return event
        raise StorageError(f"experience event not found: {ref.record_id}", stage="setup")

    def append(self, record: ExperienceEvent) -> RecordRef:
        try:
            record = _EVENT.validate_python(_EVENT.dump_python(record, mode="json"))
        except (ValueError, ValidationError) as error:
            raise StorageError(f"invalid experience event: {error}", stage="setup") from error
        content = canonical_bytes(_EVENT.dump_python(record, mode="json"))
        with self.files.locked():
            events, end, needs_newline = self._read()
            for event in events:
                if event.event_id == record.event_id:
                    if canonical_bytes(_EVENT.dump_python(event, mode="json")) != content:
                        raise StorageError(f"event_id already exists with different content: {record.event_id}", stage="setup")
                    return RecordRef(record_id=record.event_id, uri=self.filename + "#" + record.event_id)
            path = self.files.path(self.filename)
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("r+b" if path.exists() else "w+b") as stream:
                    # Only an unterminated malformed final fragment is removed.
                    # Newline-terminated corruption is reported by _read.
                    stream.truncate(end)
                    stream.seek(end)
                    if needs_newline:
                        stream.write(b"\n")
                    stream.write(content + b"\n")
                    stream.flush()
                    os.fsync(stream.fileno())
            except OSError as error:
                raise StorageError(f"cannot append experience event {record.event_id}: {error}", stage="setup") from error
        return RecordRef(record_id=record.event_id, uri=self.filename + "#" + record.event_id)

    def query(self, criteria: ExperienceQuery) -> tuple[ExperienceEvent, ...]:
        with self.files.locked():
            visible = [event for event in self._read()[0] if event.run_id == criteria.run_id and event.timestamp <= criteria.before_or_at and (criteria.episode_id is None or event.episode_id == criteria.episode_id)]
        terminal = {}
        for event in visible:
            if isinstance(event, AttemptFinishedEvent):
                terminal[(event.episode_id, event.payload.attempt_id)] = event.payload.task_id
        result = []
        for event in visible:
            task_id = None
            if isinstance(event, (AttemptStartedEvent, AttemptFinishedEvent)):
                key = (event.episode_id, event.payload.attempt_id)
                if key not in terminal:
                    continue
                task_id = event.payload.task_id
            elif isinstance(event, (CandidateEvaluatedEvent, VersionCommittedEvent)):
                key = (event.episode_id, event.payload.attempt_id)
                if key not in terminal:
                    continue
                task_id = terminal[key]
            elif isinstance(event, TaskFinishedEvent):
                task_id = event.payload.task_id
            if criteria.task_id is None or criteria.task_id == task_id:
                result.append(event)
        return tuple(result)
