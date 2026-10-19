"""Immutable Snapshot lineage and compare-and-swap stable pointers."""

from datetime import datetime, timezone
from hashlib import sha256
from math import isclose
from pathlib import Path
import re

from pydantic import ValidationError

from aer_core.contracts.storage import ArtifactStore, ExperienceStore
from aer_core.errors import CommitError, CommitFinalizationError, StorageError
from aer_core.schemas.evolution import EvolutionCandidate
from aer_core.schemas.experience import CommitRequest, VersionCommitRecord, VersionCommittedEvent
from aer_core.schemas.references import AgentSnapshot, RecordRef
from aer_core.schemas.storage import CandidateManifest, CommitJournal, SnapshotManifest, StablePointer, ValidationManifest
from aer_core.schemas.validation import ValidationReport

from ._files import FileRoot, canonical_bytes, key_path
from .experience import JsonlExperienceStore
from .layout import RunLayout


class LocalVersionStore:
    def __init__(self, root: str | Path, artifact_store: ArtifactStore, experience_store: ExperienceStore | None = None, *, scope_id: str = "default"):
        if not isinstance(scope_id, str) or not scope_id.strip():
            raise StorageError("scope_id must be a non-empty string", stage="setup")
        self.files = FileRoot(root)
        self.layout = RunLayout(root)
        self.artifact_store = artifact_store
        self.experience_store = experience_store if experience_store is not None else JsonlExperienceStore(root)
        self.pointer_path = self.layout.scope_path(scope_id)
        self.scope = self.pointer_path.rsplit("/", 1)[0]

    def _snapshot_path(self, version_id: str) -> str:
        return self.layout.named("snapshots", version_id, f"versions/manifests/{key_path(version_id)}.json",
            folder="versions", prefix="initial-snapshot" if version_id.startswith("initial-") else "snapshot")

    def _candidate_path(self, candidate_id: str) -> str:
        return self.layout.named("candidates", candidate_id, f"versions/candidates/{key_path(candidate_id)}.json",
            folder="versions", prefix="candidate-" + candidate_id.rsplit("/", 1)[-1])

    def _validation_path(self, validation_id: str) -> str:
        if self.layout.enabled:
            return self.layout.named("validations", validation_id, "", folder="validation", prefix=validation_id)
        if re.fullmatch(r"validation-\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_\d{6}", validation_id):
            return f"validation/{validation_id}.json"
        return f"validation/{key_path(validation_id)}.json"

    def _read_model(self, relative: str, model):
        try:
            return model.model_validate(self.files.read_json(relative))
        except ValidationError as error:
            raise StorageError(f"invalid {model.__name__} in {relative}: {error}", stage="setup") from error

    def _check_artifacts(self, snapshot: AgentSnapshot):
        for ref in snapshot.artifacts.values():
            self.artifact_store.get(ref)

    def save(self, snapshot: AgentSnapshot) -> RecordRef:
        snapshot = AgentSnapshot.model_validate_json(snapshot.model_dump_json())
        with self.files.locked():
            self._check_artifacts(snapshot)
            if snapshot.parent_version_id is not None:
                self.load(snapshot.parent_version_id)
            digest = sha256(canonical_bytes(snapshot.model_dump(mode="json"))).hexdigest()
            manifest = SnapshotManifest(snapshot=snapshot, snapshot_digest=digest)
            relative = self._snapshot_path(snapshot.version_id)
            self.files.immutable(relative, canonical_bytes(manifest.model_dump(mode="json")))
        return RecordRef(record_id=snapshot.version_id, uri=relative)

    def load(self, version_id: str) -> AgentSnapshot:
        with self.files.locked():
            manifest = self._read_model(self._snapshot_path(version_id), SnapshotManifest)
            snapshot = manifest.snapshot
            digest = sha256(canonical_bytes(snapshot.model_dump(mode="json"))).hexdigest()
            if snapshot.version_id != version_id or digest != manifest.snapshot_digest:
                raise StorageError("Snapshot identity or persisted content differs from its manifest", stage="setup")
            self._check_artifacts(snapshot)
            return snapshot

    def initialize(self, snapshot: AgentSnapshot) -> RecordRef:
        with self.files.locked():
            ref = self.save(snapshot)
            if self.files.path(self.pointer_path).exists():
                pointer = self._read_model(self.pointer_path, StablePointer)
                if pointer.initial_version_id != snapshot.version_id:
                    raise StorageError("scope is already initialized with a different initial version", stage="setup")
                self._recover(pointer)
                return ref
            pointer = StablePointer(initial_version_id=snapshot.version_id, version_id=snapshot.version_id)
            self.files.replace(self.pointer_path, canonical_bytes(pointer.model_dump(mode="json")))
            return ref

    def stable(self) -> AgentSnapshot:
        with self.files.locked():
            pointer = self._read_model(self.pointer_path, StablePointer)
            self._recover(pointer)
            return self.load(pointer.version_id)

    def materialize(self, stable: AgentSnapshot, candidate: EvolutionCandidate) -> AgentSnapshot:
        candidate = EvolutionCandidate.model_validate_json(candidate.model_dump_json())
        with self.files.locked():
            if stable != self.load(stable.version_id) or candidate.parent_version_id != stable.version_id:
                raise StorageError("candidate parent must match the saved stable Snapshot", stage="setup")
            seed = {"parent": stable.version_id, "candidate": candidate.candidate_id, "updates": candidate.model_dump(mode="json")["artifact_updates"]}
            version_id = candidate.candidate_version_id or "candidate-" + sha256(canonical_bytes(seed)).hexdigest()
            artifacts = dict(stable.artifacts)
            artifacts.update(candidate.artifact_updates)
            snapshot = stable.replace(version_id=version_id, parent_version_id=stable.version_id, artifacts=artifacts)
            self.save(snapshot)
            manifest = CandidateManifest(candidate_id=candidate.candidate_id, parent_version_id=stable.version_id, candidate_version_id=version_id)
            self.files.immutable(self._candidate_path(candidate.candidate_id), canonical_bytes(manifest.model_dump(mode="json")))
            return snapshot

    def save_validation(self, report: ValidationReport) -> RecordRef:
        report = ValidationReport.model_validate_json(report.model_dump_json())
        with self.files.locked():
            self.load(report.baseline_version_id)
            self.load(report.candidate_version_id)
            candidate = self._read_model(self._candidate_path(report.candidate_id), CandidateManifest)
            if (candidate.parent_version_id, candidate.candidate_version_id) != (report.baseline_version_id, report.candidate_version_id):
                raise StorageError("validation versions must match the materialized candidate", stage="setup")
            for ref in (report.local_result_ref, report.private_results_ref):
                if ref is not None:
                    self.artifact_store.get(ref)
            relative = self._validation_path(report.validation_id)
            wrapper = ValidationManifest(report=report)
            self.files.immutable(relative, canonical_bytes(wrapper.model_dump(mode="json")))
        return RecordRef(record_id=report.validation_id, uri=relative)

    def load_validation(self, ref: RecordRef) -> ValidationReport:
        if ref.uri != self._validation_path(ref.record_id):
            raise StorageError("validation reference does not belong to the local format", stage="setup")
        with self.files.locked():
            report = self._read_model(ref.uri, ValidationManifest).report
            if report.validation_id != ref.record_id:
                raise StorageError("validation record identity differs from its reference", stage="setup")
            for artifact in (report.local_result_ref, report.private_results_ref):
                if artifact is not None:
                    self.artifact_store.get(artifact)
            return report

    def _commit_key(self, record: VersionCommitRecord) -> str:
        return sha256(canonical_bytes({"run": record.run_id, "episode": record.episode_id, "attempt": record.attempt_id, "candidate": record.candidate_id})).hexdigest()

    def _journal_path(self, key: str, *, committed: bool) -> str:
        if self.layout.enabled:
            return self.layout.named("receipts" if committed else "intents", key, "", folder="versions",
                prefix="commit-receipt" if committed else "commit-intent")
        return f"{self.scope}/{'committed' if committed else 'intents'}/{key}.json"

    def _finalize(self, journal: CommitJournal):
        key = self._commit_key(journal.record)
        self.files.immutable(self._journal_path(key, committed=True), canonical_bytes(journal.model_dump(mode="json")))
        record = journal.record
        event = VersionCommittedEvent(event_id="commit-" + key, run_id=record.run_id, episode_id=record.episode_id, timestamp=record.committed_at, payload=record)
        self.experience_store.append(event)

    def _recover(self, pointer: StablePointer):
        if pointer.last_commit is None:
            return
        journal = pointer.last_commit
        intent = self._read_model(self._journal_path(self._commit_key(journal.record), committed=False), CommitJournal)
        if intent != journal:
            raise StorageError("stable pointer does not match its immutable commit intent", stage="commit")
        self.load(pointer.version_id)
        try:
            self._finalize(journal)
        except Exception as error:
            raise CommitFinalizationError("stable pointer is already committed; receipt/experience recovery is still incomplete", committed_record=journal.record) from error

    def commit(self, request: CommitRequest) -> VersionCommitRecord:
        request = CommitRequest.model_validate_json(request.model_dump_json())
        digest = sha256(canonical_bytes(request.model_dump(mode="json"))).hexdigest()
        published = None
        try:
            with self.files.locked():
                pointer = self._read_model(self.pointer_path, StablePointer)
                self._recover(pointer)
                provisional = VersionCommitRecord(run_id=request.run_id, episode_id=request.episode_id, attempt_id=request.attempt_id, candidate_id=request.candidate_id, previous_version_id=request.expected_stable_version_id, committed_version_id=request.validated_snapshot.version_id, committed_at=datetime.now(timezone.utc))
                key = self._commit_key(provisional)
                receipt_path = self._journal_path(key, committed=True)
                if self.files.path(receipt_path).exists():
                    receipt = self._read_model(receipt_path, CommitJournal)
                    if receipt.request_digest != digest:
                        raise CommitError("commit identity was reused with a different request", stage="commit")
                    self._finalize(receipt)
                    return receipt.record
                if pointer.version_id != request.expected_stable_version_id:
                    raise CommitError("stable version changed; candidate is based on a stale parent", stage="commit")
                snapshot = self.load(request.validated_snapshot.version_id)
                if snapshot != request.validated_snapshot:
                    raise CommitError("submitted Snapshot differs from the saved validation version", stage="commit")
                candidate = self._read_model(self._candidate_path(request.candidate_id), CandidateManifest)
                if (candidate.parent_version_id, candidate.candidate_version_id) != (request.expected_stable_version_id, snapshot.version_id):
                    raise CommitError("commit candidate does not match the materialized version", stage="commit")
                report = self.load_validation(request.validation_ref)
                if report.status != "complete" or (report.candidate_id, report.baseline_version_id, report.candidate_version_id) != (request.candidate_id, request.expected_stable_version_id, snapshot.version_id):
                    raise CommitError("commit requires complete validation for these exact versions", stage="commit")
                baseline, updated = report.baseline_metrics, report.candidate_metrics
                gains = (updated.score - baseline.score, updated.similar_score - baseline.similar_score, updated.regression_score - baseline.regression_score)
                supplied = (request.acceptance.delta_score, request.acceptance.delta_similar_score, request.acceptance.delta_regression_score)
                if any(not isclose(actual, claimed, abs_tol=1e-12) for actual, claimed in zip(gains, supplied)):
                    raise CommitError("acceptance gains do not match the saved validation report", stage="commit")
                intent_path = self._journal_path(key, committed=False)
                if self.files.path(intent_path).exists():
                    journal = self._read_model(intent_path, CommitJournal)
                    if journal.request_digest != digest:
                        raise CommitError("pending commit identity was reused with different content", stage="commit")
                else:
                    journal = CommitJournal(request_digest=digest, record=provisional, validation_ref=request.validation_ref)
                    self.files.immutable(intent_path, canonical_bytes(journal.model_dump(mode="json")))
                updated_pointer = pointer.replace(version_id=snapshot.version_id, last_commit=journal)
                self.files.replace(self.pointer_path, canonical_bytes(updated_pointer.model_dump(mode="json")))
                published = journal.record
                self._finalize(journal)
                return journal.record
        except CommitError:
            raise
        except Exception as error:
            if published is not None:
                raise CommitFinalizationError("stable pointer was committed; retry this request to finish receipt/experience persistence", committed_record=published) from error
            raise CommitError(f"commit could not be published: {error}", stage="commit") from error
