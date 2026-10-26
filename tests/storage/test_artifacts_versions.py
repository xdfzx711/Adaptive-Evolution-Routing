import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from aer_core.errors import CommitError, CommitFinalizationError, StorageError
from aer_core.schemas.experience import CandidateEvaluatedEvent, VersionCommittedEvent
from aer_core.schemas.references import RecordRef
from tests.domain_helpers import NOW
from tests.storage.helpers import commit_request, initial, outcome, prepare_candidate, stores, validation


class LocalStorageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.artifacts, self.versions, self.experience = stores(self.root)
        self.stable = initial(self.artifacts, self.versions)

    def test_artifacts_are_content_addressed_and_reloadable(self):
        ref = self.artifacts.put("你好".encode(), "prompt")
        self.assertRegex(ref.uri, r"^artifacts/prompt-\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_\d{6}\.bin$")
        self.assertEqual(self.artifacts.put("你好".encode(), "prompt"), ref)
        reopened, _, _ = stores(self.root)
        self.assertEqual(reopened.get(ref), "你好".encode())
        self.assertNotEqual(self.artifacts.put("你好".encode(), "memory").artifact_id, ref.artifact_id)

    def test_legacy_hash_references_and_frozen_imports_remain_readable(self):
        from plugins.storage.artifacts import LocalArtifactStore
        raw = b"legacy immutable instructions"
        legacy = self.artifacts._ref(raw, "prompt")
        self.artifacts.files.immutable(legacy.uri, raw)
        self.assertEqual(self.artifacts.get(legacy), raw)
        self.assertEqual(self.artifacts.put(raw, "prompt"), legacy)
        modern = self.artifacts.put(b"timestamp instructions", "prompt")
        imported = LocalArtifactStore(self.root / "imported")
        for ref, data in ((legacy, raw), (modern, b"timestamp instructions")):
            self.assertEqual(imported.import_ref(ref, data), ref)
            self.assertEqual(imported.get(ref), data)
            self.assertEqual(imported.put(data, "prompt"), ref)

    def test_artifact_content_identity_is_independent_of_timestamp_location(self):
        from plugins.storage.artifacts import LocalArtifactStore
        first = self.artifacts.put(b"shared content", "memory")
        second = LocalArtifactStore(self.root / "another").put(b"shared content", "memory")
        self.assertNotEqual(first.uri, second.uri)
        self.assertEqual(first.artifact_id, second.artifact_id)
        self.assertEqual(first.digest, second.digest)

    def test_corrupt_or_forged_artifact_is_not_overwritten(self):
        ref = self.stable.artifacts["prompt"]
        with self.assertRaises(StorageError):
            self.artifacts.get(ref.replace(uri="../outside.bin"))
        path = self.root / ref.uri
        path.write_bytes(b"corrupted")
        with self.assertRaises(StorageError):
            self.artifacts.get(ref)
        with self.assertRaises(StorageError):
            self.artifacts.put(b"original prompt", "prompt")
        self.assertEqual(path.read_bytes(), b"corrupted")

    def test_snapshot_reload_immutability_and_version_collision(self):
        _, versions, _ = stores(self.root)
        loaded = versions.load("v1")
        self.assertEqual(loaded, self.stable)
        with self.assertRaises(TypeError):
            loaded.config["nested"]["steps"] += (3,)
        with self.assertRaises(StorageError):
            versions.save(self.stable.replace(config={"changed": True}))
        self.assertEqual(versions.load("v1"), self.stable)

    def test_candidate_and_rejection_leave_stable_pointer_and_prompt_unchanged(self):
        before = (self.root / self.versions.pointer_path).read_bytes()
        candidate, snapshot = prepare_candidate(self.artifacts, self.versions, self.stable)
        _, ref = validation(self.artifacts, self.versions, self.stable, snapshot)
        event = CandidateEvaluatedEvent(event_id="rejected", run_id="run-1", episode_id="episode-1", timestamp=NOW, payload=outcome(ref, accepted=False))
        self.experience.append(event)
        self.assertEqual((self.root / self.versions.pointer_path).read_bytes(), before)
        self.assertEqual(self.artifacts.get(self.stable.artifacts["prompt"]), b"original prompt")
        self.assertEqual(self.versions.stable(), self.stable)
        _, reopened, experience = stores(self.root)
        self.assertEqual(reopened.load(snapshot.version_id).parent_version_id, "v1")
        self.assertFalse(experience.events()[0].payload.acceptance.accepted)

    def test_materialize_is_repeatable_and_rejects_reused_candidate_identity(self):
        candidate, snapshot = prepare_candidate(self.artifacts, self.versions, self.stable)
        self.assertEqual(self.versions.materialize(self.stable, candidate), snapshot)
        with self.assertRaises(StorageError):
            self.versions.materialize(self.stable.replace(config={}), candidate)
        replacement = candidate.replace(artifact_updates={"prompt": self.artifacts.put(b"different", "prompt")}, candidate_version_id="v-other")
        with self.assertRaises(StorageError):
            self.versions.materialize(self.stable, replacement)
        self.assertEqual(self.versions.stable().version_id, "v1")

    def test_commit_reload_and_retry_preserve_actual_commit_once(self):
        request, report = commit_request(self.artifacts, self.versions, self.stable)
        record = self.versions.commit(request)
        self.assertEqual(self.versions.commit(request), record)
        _, reopened, experience = stores(self.root)
        self.assertEqual(reopened.stable().version_id, "v2")
        self.assertEqual(reopened.load("v1"), self.stable)
        self.assertEqual(reopened.load_validation(request.validation_ref), report)
        commits = [event for event in experience.events() if isinstance(event, VersionCommittedEvent)]
        self.assertEqual(len(commits), 1)
        self.assertEqual(commits[0].payload, record)

    def test_stale_parent_or_changed_validation_receipt_cannot_commit(self):
        first, _ = commit_request(self.artifacts, self.versions, self.stable)
        stale, _ = commit_request(self.artifacts, self.versions, self.stable, "candidate-stale", "v-stale", "attempt-stale")
        self.versions.commit(first)
        with self.assertRaises(CommitError):
            self.versions.commit(stale)
        with self.assertRaises(CommitError):
            self.versions.commit(first.replace(validation_ref=RecordRef(record_id="other", uri="other.json")))
        self.assertEqual(self.versions.stable().version_id, "v2")

    def test_exact_snapshot_and_existing_validation_required(self):
        request, _ = commit_request(self.artifacts, self.versions, self.stable)
        for altered in [request.replace(validated_snapshot=request.validated_snapshot.replace(config={"tampered": True})), request.replace(validation_ref=RecordRef(record_id="missing", uri="validation/missing.json"))]:
            with self.subTest(request=altered):
                with self.assertRaises(CommitError):
                    self.versions.commit(altered)
        self.assertEqual(self.versions.stable().version_id, "v1")

    def test_incomplete_validation_cannot_be_promoted(self):
        request, report = commit_request(self.artifacts, self.versions, self.stable)
        incomplete = report.replace(validation_id="incomplete", status="incomplete", candidate_metrics=None, candidate_validation_tokens=None)
        ref = self.versions.save_validation(incomplete)
        with self.assertRaises(CommitError):
            self.versions.commit(request.replace(validation_ref=ref))
        self.assertEqual(self.versions.stable().version_id, "v1")

    def test_claimed_reward_must_match_persisted_metrics(self):
        request, _ = commit_request(self.artifacts, self.versions, self.stable)
        false_gain = request.acceptance.replace(reward=0.1, delta_score=0.1)
        with self.assertRaises(CommitError):
            self.versions.commit(request.replace(acceptance=false_gain))
        self.assertEqual(self.versions.stable().version_id, "v1")

    def test_weighted_current_task_gain_must_match_persisted_metrics(self):
        from aer_core.schemas.validation import LocalValidationMetrics
        from plugins.validation.acceptance import WeightedImprovement
        from tests.validation.helpers import settings
        request, report = commit_request(self.artifacts, self.versions, self.stable)
        report = report.replace(validation_id="weighted-validation", candidate_metrics=report.baseline_metrics,
            local_metrics=LocalValidationMetrics(baseline_score=0, candidate_score=0.52, baseline_success=False,
                candidate_success=False, delta_score=0.52, repaired=False))
        ref = self.versions.save_validation(report)
        policy = WeightedImprovement(settings(acceptance_policy="weighted_improvement", max_regression_drop=None))
        forged = report.replace(local_metrics=report.local_metrics.replace(candidate_score=1, candidate_success=True,
            delta_score=1, repaired=True))
        with self.assertRaises(CommitError):
            self.versions.commit(request.replace(validation_ref=ref, acceptance=policy.evaluate(forged)))
        self.assertEqual(self.versions.stable().version_id, "v1")
        self.versions.commit(request.replace(validation_ref=ref, acceptance=policy.evaluate(report)))
        self.assertEqual(self.versions.stable().version_id, "v2")

    def test_pointer_write_failure_leaves_old_stable_and_retry_uses_saved_intent(self):
        request, _ = commit_request(self.artifacts, self.versions, self.stable)
        publish = self.versions.files.replace
        def fail_pointer(relative, content):
            if relative == self.versions.pointer_path:
                raise StorageError("injected write failure", stage="commit")
            publish(relative, content)
        with patch.object(self.versions.files, "replace", side_effect=fail_pointer):
            with self.assertRaises(CommitError):
                self.versions.commit(request)
        self.assertEqual(self.versions.stable().version_id, "v1")
        self.assertFalse(any(isinstance(event, VersionCommittedEvent) for event in self.experience.events()))
        self.assertEqual(self.versions.commit(request).committed_version_id, "v2")

    def test_log_failure_after_pointer_publication_is_recovered(self):
        request, _ = commit_request(self.artifacts, self.versions, self.stable)
        with patch.object(self.experience, "append", side_effect=StorageError("injected log failure", stage="commit")):
            with self.assertRaises(CommitFinalizationError) as caught:
                self.versions.commit(request)
            with self.assertRaises(CommitFinalizationError) as retried:
                self.versions.commit(request)
            self.assertEqual(retried.exception.committed_record, caught.exception.committed_record)
        self.assertEqual(caught.exception.committed_record.committed_version_id, "v2")
        _, reopened, experience = stores(self.root)
        self.assertEqual(reopened.stable().version_id, "v2")
        self.assertEqual(reopened.commit(request), caught.exception.committed_record)
        self.assertEqual(len(experience.events()), 1)

    def test_missing_receipt_recovers_from_pointer_witness(self):
        request, _ = commit_request(self.artifacts, self.versions, self.stable)
        record = self.versions.commit(request)
        key = self.versions._commit_key(record)
        receipt = self.root / self.versions._journal_path(key, committed=True)
        receipt.unlink()
        _, reopened, experience = stores(self.root)
        self.assertEqual(reopened.stable().version_id, "v2")
        self.assertTrue(receipt.exists())
        self.assertEqual(len(experience.events()), 1)

    def test_retry_of_older_commit_does_not_reset_newer_stable(self):
        first, _ = commit_request(self.artifacts, self.versions, self.stable)
        old_record = self.versions.commit(first)
        second, _ = commit_request(self.artifacts, self.versions, self.versions.stable(), "candidate-next", "v3", "attempt-next")
        self.versions.commit(second)
        self.assertEqual(self.versions.commit(first), old_record)
        self.assertEqual(self.versions.stable().version_id, "v3")
        self.assertEqual(len(self.experience.events()), 2)

    def test_separate_scopes_share_manifests_but_not_stable_pointers(self):
        _, separate, _ = stores(self.root, scope="other-episode")
        separate.initialize(self.stable)
        request, _ = commit_request(self.artifacts, self.versions, self.stable)
        self.versions.commit(request)
        self.assertEqual(separate.stable().version_id, "v1")
        self.assertEqual(self.versions.stable().version_id, "v2")

    def test_racing_candidates_only_one_can_promote_from_the_same_parent(self):
        one, _ = commit_request(self.artifacts, self.versions, self.stable)
        two, _ = commit_request(self.artifacts, self.versions, self.stable, "candidate-two", "v-two", "attempt-two")
        def promote(request):
            _, versions, _ = stores(self.root)
            try:
                return versions.commit(request).committed_version_id
            except CommitError:
                return None
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(promote, [one, two]))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertIn(self.versions.stable().version_id, {"v2", "v-two"})

    def test_cross_process_lock_rejects_a_second_writer(self):
        script = """
import sys
from aer_core.errors import StorageError
from plugins.storage.artifacts import LocalArtifactStore
try:
    LocalArtifactStore(sys.argv[1]).put(b'other', 'prompt')
except StorageError:
    sys.exit(0)
sys.exit(1)
"""
        with self.artifacts.files.locked():
            result = subprocess.run([sys.executable, "-c", script, str(self.root)], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
