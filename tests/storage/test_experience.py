import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from aer_core.errors import StorageError
from aer_core.schemas.execution import ErrorInfo
from aer_core.schemas.experience import AttemptFinishedEvent, AttemptStartedEvent, CandidateEvaluatedEvent, ExperienceQuery, TaskFinishedEvent
from aer_core.schemas.references import RecordRef
from plugins.storage.experience import JsonlExperienceStore
from tests.domain_helpers import NOW, run_result
from tests.storage.helpers import attempt, outcome


class ExperienceStorageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = JsonlExperienceStore(self.root)

    def finished(self, payload=None, event_id="finished", timestamp=NOW):
        payload = payload or attempt()
        return AttemptFinishedEvent(event_id=event_id, run_id=payload.run_id, episode_id=payload.episode_id, timestamp=timestamp, payload=payload)

    def test_empty_and_exception_attempts_are_preserved_without_candidates(self):
        empty = attempt("empty_candidates", ())
        failed = attempt().replace(status="error", candidate_ids=[], decision=None, operator_id=None, error=ErrorInfo(code="routing_error", stage="routing", message="failed before candidates"))
        self.store.append(self.finished(empty, "empty"))
        self.store.append(self.finished(failed, "exception"))
        events = JsonlExperienceStore(self.root).events()
        self.assertEqual([event.payload.status.value for event in events], ["empty_candidates", "error"])
        self.assertTrue(all(not event.payload.candidate_ids for event in events))

    def test_accepted_rejected_and_interrupted_candidate_records_are_retained(self):
        ref = RecordRef(record_id="validation-1", uri="validation.json")
        accepted = outcome(ref)
        rejected = outcome(ref, accepted=False)
        incomplete = accepted.replace(acceptance=None, status="incomplete", selected=False, committed=False)
        for name, payload in [("accepted", accepted), ("rejected", rejected), ("incomplete", incomplete)]:
            self.store.append(CandidateEvaluatedEvent(event_id=name, run_id="run-1", episode_id="episode-1", timestamp=NOW, payload=payload))
        events = self.store.events()
        self.assertEqual([event.payload.status.value for event in events], ["accepted", "rejected", "incomplete"])

    def test_append_retry_is_idempotent_and_conflicting_id_is_rejected(self):
        event = self.finished()
        ref = self.store.append(event)
        self.assertEqual(self.store.append(event), ref)
        self.assertEqual(self.store.get(ref), event)
        self.assertEqual(len(self.store.events()), 1)
        with self.assertRaises(StorageError):
            self.store.append(event.replace(timestamp=NOW + timedelta(seconds=1)))
        with self.assertRaises(StorageError):
            self.store.get(ref.replace(uri="other.jsonl"))

    def test_query_has_no_future_or_unfinished_attempts(self):
        completed = attempt()
        started = completed.replace(status="started", decision=None, operator_id=None, candidate_ids=[], budget_after=None, finished_at=None)
        start = AttemptStartedEvent(event_id="started", run_id="run-1", episode_id="episode-1", timestamp=NOW, payload=started)
        self.store.append(start)
        before = ExperienceQuery(run_id="run-1", episode_id="episode-1", before_or_at=NOW)
        self.assertEqual(self.store.query(before), ())
        self.assertEqual(self.store.events(), (start,))
        ended = completed.replace(finished_at=NOW + timedelta(seconds=1))
        finish = self.finished(ended, timestamp=NOW + timedelta(seconds=1))
        self.store.append(finish)
        self.assertEqual(self.store.query(before), ())
        after = before.replace(before_or_at=NOW + timedelta(seconds=1))
        self.assertEqual(self.store.query(after), (start, finish))

    def test_candidate_queries_join_attempt_identity_and_filter_task_episode_run(self):
        finished = self.finished()
        candidate = CandidateEvaluatedEvent(event_id="candidate", run_id="run-1", episode_id="episode-1", timestamp=NOW, payload=outcome(RecordRef(record_id="validation", uri="validation.json")))
        self.store.append(candidate)
        criteria = ExperienceQuery(run_id="run-1", episode_id="episode-1", task_id="dev-001", before_or_at=NOW)
        self.assertEqual(self.store.query(criteria), ())
        self.store.append(finished)
        self.assertEqual(self.store.query(criteria), (candidate, finished))
        for update in [{"task_id": "other"}, {"episode_id": "other"}, {"run_id": "other"}]:
            self.assertEqual(self.store.query(criteria.replace(**update)), ())

    def test_task_finished_is_available_without_any_evolution(self):
        value = run_result()
        event = TaskFinishedEvent(event_id="task", run_id=value.run_id, episode_id=value.episode_id, timestamp=NOW, payload=value)
        self.store.append(event)
        self.assertEqual(self.store.query(ExperienceQuery(run_id=value.run_id, before_or_at=NOW)), (event,))

    def test_interrupted_last_write_is_ignored_then_repaired_on_next_append(self):
        first = self.finished(event_id="first")
        self.store.append(first)
        path = self.root / self.store.filename
        with path.open("ab") as stream:
            stream.write(b'{"event_id":"broken')
        self.assertEqual(self.store.events(), (first,))
        second = self.finished(event_id="second")
        self.store.append(second)
        self.assertEqual(self.store.events(), (first, second))
        self.assertEqual(len(path.read_bytes().splitlines()), 2)

    def test_complete_json_without_final_newline_is_preserved(self):
        first = self.finished(event_id="first")
        self.store.append(first)
        path = self.root / self.store.filename
        path.write_bytes(path.read_bytes().rstrip(b"\n"))
        second = self.finished(event_id="second")
        self.store.append(second)
        self.assertEqual(self.store.events(), (first, second))

    def test_newline_terminated_corruption_is_not_silently_removed(self):
        self.store.append(self.finished())
        path = self.root / self.store.filename
        with path.open("ab") as stream:
            stream.write(b'{"broken":\n')
        corrupted = path.read_bytes()
        with self.assertRaises(StorageError):
            self.store.append(self.finished(event_id="later"))
        self.assertEqual(path.read_bytes(), corrupted)
