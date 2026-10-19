import importlib
import json
import unittest
from datetime import timedelta
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from aer_core.schemas.base import DomainModel
from aer_core.schemas.evolution import EvolutionCandidate, EvolutionDecision, EvolutionRequest, EvolutionRequestRecord, EvolutionResponse, ExperienceSummary
from aer_core.schemas.execution import Diagnosis, ErrorInfo, TraceEvent
from aer_core.schemas.experience import (
    AttemptFinishedEvent, AttemptStartedEvent, CandidateEvaluatedEvent,
    CandidateEvaluation, CommitRequest, EvolutionAttempt, ExperienceEvent,
    TaskFinishedEvent, VersionCommitRecord, VersionCommittedEvent,
)
from aer_core.schemas.experiment import ExperimentResult
from aer_core.schemas.providers import ModelMessage, ModelRequest, ModelResponse, ToolCall, ToolResult, ToolSpec
from aer_core.schemas.references import AgentSnapshot, ManifestRef, RecordRef
from aer_core.schemas.runtime import RunContext, RunContextRecord
from aer_core.schemas.states import CandidateStatus, RunStatus
from aer_core.schemas.task import EvaluationSpec, TaskView
from aer_core.schemas.usage import Reservation, ResourceUsage, UsageReport
from aer_core.schemas.validation import PairSeed, ValidationMetrics, ValidationPlan
from tests.domain_helpers import (
    NOW, acceptance, artifact, context, evaluation, execution, remaining,
    report, run_context, run_result, snapshot, task, task_view,
)


class DomainTests(unittest.TestCase):
    def assert_roundtrip(self, value):
        encoded = value.model_dump_json()
        self.assertEqual(type(value).model_validate_json(encoded), value)
        self.assertEqual(type(value).model_validate(json.loads(encoded)), value)
        return encoded

    def candidate(self):
        return EvolutionCandidate(candidate_id="candidate-1", parent_version_id="v1", target="prompt", artifact_updates={"prompt": artifact(version="v2")}, candidate_version_id="v2")

    def candidate_evaluation(self):
        return CandidateEvaluation(attempt_id="attempt-1", candidate_id="candidate-1", candidate_version_id="v2", validation_ref=RecordRef(record_id="validation-1", uri="validation.json"), acceptance=acceptance(), status="committed", selected=True, committed=True, commit_error=None, final_recorded_at=NOW)

    def attempt(self):
        return EvolutionAttempt(attempt_id="attempt-1", run_id="run-1", episode_id="episode-1", task_id="dev-001", round_id=1, stable_version_id="v1", pre_state_ref=artifact("state"), trajectory_ref=artifact("trace"), feasible_targets=["prompt"], decision=EvolutionDecision(target="prompt", rationale="fixed"), operator_id="mock_evolver", candidate_ids=["candidate-1"], status="completed", error=None, budget_before=remaining(), budget_after=remaining(), usage=ResourceUsage.zero(), started_at=NOW, finished_at=NOW)

    def test_basic_domain_values_roundtrip(self):
        for value in [task(), task_view(), snapshot(), execution(), evaluation(), context(), self.candidate(), report(), acceptance(), self.attempt(), self.candidate_evaluation(), run_result(), ErrorInfo(code="error", stage="evolution", message="failed"), Diagnosis(reason_code="unknown", explanation="hypothesis")]:
            with self.subTest(model=type(value).__name__):
                self.assert_roundtrip(value)

    def test_snapshot_is_deeply_immutable_and_copies_input(self):
        source = {"nested": {"values": [1, {"value": "old"}]}}
        stable = AgentSnapshot(version_id="v1", agent_plugin_id="mock", config=source, artifacts={"prompt": artifact()})
        source["nested"]["values"][1]["value"] = "new"
        self.assertEqual(stable.config["nested"]["values"][1]["value"], "old")
        with self.assertRaises(TypeError):
            stable.config["nested"]["values"][1]["value"] = "mutated"
        with self.assertRaises(TypeError):
            stable.artifacts["prompt"] = artifact(version="v2")
        with self.assertRaises(ValidationError):
            stable.version_id = "v2"
        self.assert_roundtrip(stable)

    def test_defaults_and_serialized_copies_do_not_mutate_domain_values(self):
        value = task().replace(metadata={})
        with self.assertRaises(TypeError):
            value.metadata["x"] = 1
        candidate = self.candidate()
        with self.assertRaises(TypeError):
            candidate.metadata["x"] = 1
        plain = snapshot().model_dump()
        plain["config"]["nested"]["steps"].append(9)
        self.assertEqual(snapshot().config["nested"]["steps"], (1, 2))

    def test_domain_json_rejects_non_json_values_and_nonfinite_numbers(self):
        for value in [object(), {"nested": [float("nan")]}, {"nested": float("inf")}]:
            with self.subTest(value=repr(value)):
                with self.assertRaises(ValidationError):
                    snapshot().replace(config={"value": value})
        with self.assertRaises(ValidationError):
            evaluation().replace(score=1.1)
        with self.assertRaises(ValidationError):
            execution().replace(unexpected_field=1)

    def test_trace_order_and_evidence_references(self):
        with self.assertRaises(ValidationError):
            execution().replace(trajectory=[TraceEvent(seq=1, kind="finish", payload={}), TraceEvent(seq=1, kind="finish", payload={})])
        with self.assertRaises(ValidationError):
            context().replace(diagnosis=Diagnosis(reason_code="unknown", explanation="", evidence_step_ids=[99]))

    def test_public_views_do_not_accept_private_evaluation_fields(self):
        with self.assertRaises(ValidationError):
            TaskView(**task_view().model_dump(), eval_spec_ref="private.json")
        with self.assertRaises(ValidationError):
            context().replace(evaluation=evaluation())

    def test_context_rejects_wrong_version_task_and_future_history(self):
        with self.assertRaises(ValidationError):
            context().replace(stable_snapshot=snapshot("v2"))
        with self.assertRaises(ValidationError):
            context().replace(task=task_view().replace(task_id="other"))
        future = ExperienceSummary(attempt_id="old", run_id="run-1", episode_id="episode-1", task_id="dev-001", round_id=1, stable_version_id="v1", decision=None, diagnosis=None, candidate_summaries=[], completed_at=NOW + timedelta(seconds=1))
        with self.assertRaises(ValidationError):
            context().replace(history=[future])

    def test_live_context_persists_only_a_record(self):
        live = run_context()
        record = live.to_record()
        encoded = self.assert_roundtrip(record)
        self.assertNotIn('"budget"', encoded)
        self.assertIn('"remaining_budget"', encoded)
        restored = RunContext.from_record(RunContextRecord.model_validate_json(encoded), budget=live.budget)
        self.assertEqual(restored.to_record(), record)

    def test_request_serialization_and_reattachment(self):
        live = run_context()
        request = EvolutionRequest(attempt_id="attempt-1", round_id=1, context=context(), decision=EvolutionDecision(target="prompt", rationale="fixed"), operator_id="mock_evolver", max_candidates=1, run_context=live)
        record = request.to_record()
        encoded = self.assert_roundtrip(record)
        restored = EvolutionRequest.from_record(EvolutionRequestRecord.model_validate_json(encoded), run_context=live)
        self.assertEqual(restored.to_record(), record)
        with self.assertRaises(ValidationError):
            record.replace(decision=EvolutionDecision(target="memory", rationale="invalid"))

    def test_usage_unknown_is_distinct_from_zero(self):
        unknown = ResourceUsage(input_tokens=None, output_tokens=None, total_tokens=None, model_calls=1, tool_calls=0, elapsed_seconds=1, usage_complete=False, usage_sources=["unknown"])
        self.assert_roundtrip(unknown)
        with self.assertRaises(ValidationError):
            unknown.replace(usage_complete=True)
        with self.assertRaises(ValidationError):
            ResourceUsage.zero().replace(total_tokens=1)
        reservation = Reservation(operation_id="op-1", stage="evolution", reserved_tokens=10, reserved_tool_calls=0, status="pending")
        with self.assertRaises(ValidationError):
            reservation.replace(status="settled")

    def test_candidate_generation_and_empty_response_states(self):
        self.assert_roundtrip(EvolutionResponse(candidates=[self.candidate()], usage=ResourceUsage.zero(), status="completed"))
        self.assert_roundtrip(EvolutionResponse(candidates=[], usage=ResourceUsage.zero(), status="empty"))
        for payload in [dict(candidates=[], usage=ResourceUsage.zero(), status="completed"), dict(candidates=[self.candidate(), self.candidate()], usage=ResourceUsage.zero(), status="completed")]:
            with self.assertRaises(ValidationError):
                EvolutionResponse(**payload)
        with self.assertRaises(ValidationError):
            self.candidate().replace(artifact_updates={"memory": self.candidate().artifact_updates["prompt"]})

    def test_validation_plan_pairing_and_local_separation(self):
        similar, regression = task("validation-similar"), task("validation-regression")
        plan = ValidationPlan(current_task=task(), baseline_execution=execution(), baseline_evaluation=evaluation(), similar_tasks=[similar], regression_tasks=[regression], pair_seeds=[PairSeed(task=similar.identity, seed=1), PairSeed(task=regression.identity, seed=2)], policy_id="improvement_with_regression_guard", policy_version="v0.1")
        self.assert_roundtrip(plan)
        with self.assertRaises(ValidationError):
            plan.replace(regression_tasks=[similar])
        with self.assertRaises(ValidationError):
            plan.replace(similar_tasks=[task()])
        with self.assertRaises(ValidationError):
            plan.replace(pair_seeds=[])

    def test_weighted_metric_and_complete_report_requirements(self):
        weighted = ValidationMetrics(score=0.25, similar_score=1, regression_score=0, success_rate=0.25, regression_failure_count=0, task_count=4, similar_count=1, regression_count=3)
        self.assert_roundtrip(weighted)
        with self.assertRaises(ValidationError):
            weighted.replace(score=0.5)
        for update in [{"candidate_metrics": None}, {"local_result_ref": None}, {"candidate_validation_tokens": None}, {"candidate_validation_tokens": 3}]:
            with self.subTest(update=update):
                with self.assertRaises(ValidationError):
                    report().replace(**update)

    def test_acceptance_reward_and_candidate_state_relationships(self):
        rejected = acceptance(False)
        self.assert_roundtrip(rejected)
        with self.assertRaises(ValidationError):
            rejected.replace(reward=0.1)
        with self.assertRaises(ValidationError):
            self.candidate_evaluation().replace(selected=False)
        with self.assertRaises(ValidationError):
            self.candidate_evaluation().replace(acceptance=acceptance().replace(candidate_id="other"))
        failed = self.candidate_evaluation().replace(committed=False, status=CandidateStatus.COMMIT_FAILED, commit_error=ErrorInfo(code="commit_error", stage="commit", message="failed"))
        self.assert_roundtrip(failed)
        self.assertTrue(failed.acceptance.accepted)
        self.assertFalse(failed.committed)

    def test_unknown_reward_for_incomplete_candidate(self):
        rejected = acceptance(False).replace(reward=None, delta_score=None, delta_similar_score=None, delta_regression_score=None)
        incomplete = self.candidate_evaluation().replace(acceptance=rejected, status="incomplete", selected=False, committed=False)
        self.assert_roundtrip(incomplete)

    def test_final_version_cannot_reuse_old_task_evaluation(self):
        value = run_result()
        for update in [{"final_execution": execution()}, {"final_evaluation": evaluation()}, {"final_evaluation": value.final_evaluation.replace(execution_operation_id="other-op")}, {"final_evaluation_status": "not_evaluated"}]:
            with self.subTest(update=update):
                with self.assertRaises(ValidationError):
                    value.replace(**update)
        pending = value.replace(final_evaluation_status="not_evaluated", final_execution=None, final_evaluation=None, last_evaluated_execution=execution(), last_evaluated_evaluation=evaluation(), status=RunStatus.BUDGET_INTERRUPTED, stop_reason="budget_exhausted")
        self.assert_roundtrip(pending)
        self.assertEqual(pending.last_evaluated_execution.agent_version_id, "v1")

    def test_attempt_records_empty_and_pre_candidate_errors(self):
        empty = self.attempt().replace(candidate_ids=[], status="empty_candidates")
        self.assert_roundtrip(empty)
        failed = self.attempt().replace(candidate_ids=[], decision=None, operator_id=None, status="error", error=ErrorInfo(code="routing_error", stage="routing", message="failed"))
        self.assert_roundtrip(failed)
        with self.assertRaises(ValidationError):
            self.attempt().replace(finished_at=NOW - timedelta(seconds=1))

    def test_typed_events_roundtrip_and_reference_graph(self):
        adapter = TypeAdapter(ExperienceEvent)
        finished = self.attempt()
        started = finished.replace(status="started", decision=None, operator_id=None, candidate_ids=[], budget_after=None, finished_at=None)
        commit = VersionCommitRecord(run_id="run-1", episode_id="episode-1", attempt_id="attempt-1", candidate_id="candidate-1", previous_version_id="v1", committed_version_id="v2", committed_at=NOW)
        common = dict(run_id="run-1", episode_id="episode-1", timestamp=NOW)
        events = [AttemptStartedEvent(event_id="event-1", payload=started, **common), AttemptFinishedEvent(event_id="event-2", payload=finished, **common), CandidateEvaluatedEvent(event_id="event-3", payload=self.candidate_evaluation(), **common), VersionCommittedEvent(event_id="event-4", payload=commit, **common), TaskFinishedEvent(event_id="event-5", payload=run_result(), **common)]
        for event in events:
            with self.subTest(event=event.record_type):
                encoded = adapter.dump_json(event)
                self.assertEqual(adapter.validate_json(encoded), event)
        self.assertEqual(finished.candidate_ids[0], self.candidate_evaluation().candidate_id)
        self.assertEqual(self.candidate_evaluation().validation_ref.record_id, report().validation_id)
        self.assertEqual(commit.committed_version_id, run_result().final_stable_version_id)
        with self.assertRaises(ValidationError):
            events[1].replace(episode_id="other")

    def test_commit_request_checks_parent_and_candidate_acceptance(self):
        request = CommitRequest(run_id="run-1", episode_id="episode-1", attempt_id="attempt-1", candidate_id="candidate-1", expected_stable_version_id="v1", validated_snapshot=snapshot("v2", "v1"), validation_ref=RecordRef(record_id="validation-1", uri="validation.json"), acceptance=acceptance())
        self.assert_roundtrip(request)
        with self.assertRaises(ValidationError):
            request.replace(expected_stable_version_id="other")
        with self.assertRaises(ValidationError):
            request.replace(acceptance=acceptance(False))

    def test_experiment_and_provider_dtos_roundtrip(self):
        result = ExperimentResult(task_id="dev-001", method_id="mock-fixed", run_id="run-1", episode_id="episode-1", seed=42, mode="fixed", task_mode="independent", initial_version_id="v1", final_stable_version_id="v2", run_result_ref=RecordRef(record_id="task-result", uri="task.json"), eval_metrics={"score": 1}, resource_usage=ResourceUsage.zero(), trace_refs=[], artifact_refs=[artifact()], provenance=ManifestRef(record_id="manifest-1", uri="manifest.json"))
        spec = EvaluationSpec(task=task().identity, scoring_method="exact_match", private_parameters={"answer": "2"})
        tool = ToolSpec(name="calculator", description="arithmetic", parameters={"type": "object"})
        call = ToolCall(call_id="call-1", name="calculator", arguments={"expression": "1 + 1"})
        usage = UsageReport(input_tokens=0, output_tokens=0, model_calls=0, tool_calls=1, elapsed_seconds=0.1, usage_source="simulated")
        values = [result, spec, ModelRequest(messages=[ModelMessage(role="user", content="hello")], tools=[tool], seed=42, max_output_tokens=10), ModelResponse(content=None, tool_calls=[call], finish_reason="tool_calls", usage=usage), ToolResult(call_id="call-1", name="calculator", output={"answer": 2}, status="completed", usage=usage)]
        for value in values:
            self.assert_roundtrip(value)

    def test_utc_and_all_domain_json_schemas(self):
        self.assertEqual(execution().trajectory[0].timestamp.utcoffset(), timedelta(0))
        with self.assertRaises(ValidationError):
            execution().trajectory[0].replace(timestamp=NOW.replace(tzinfo=None))
        for path in (Path(__file__).resolve().parents[2] / "aer_core/schemas").glob("*.py"):
            module = importlib.import_module("aer_core.schemas." + path.stem)
            for obj in vars(module).values():
                if isinstance(obj, type) and issubclass(obj, DomainModel) and obj.__module__ == module.__name__:
                    with self.subTest(schema=obj.__name__):
                        self.assertEqual(obj.model_json_schema()["type"], "object")
