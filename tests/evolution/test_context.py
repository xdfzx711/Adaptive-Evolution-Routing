import unittest
from datetime import timedelta

from aer_core.context import ContextBuilder
from aer_core.errors import ContractError, NoFeasibleTargets
from aer_core.schemas.evolution import EvolutionDecision
from aer_core.schemas.execution import TraceEvent
from aer_core.schemas.experience import AttemptFinishedEvent, CandidateChange, CandidateEvaluatedEvent, CandidateEvaluation, TaskFinishedEvent
from aer_core.schemas.history import ComponentEdit
from tests.domain_helpers import NOW, acceptance, run_result
from tests.evolution.helpers import MemoryExperience, fixture
from tests.storage.helpers import attempt


CANARY = "PRIVATE_LABEL_DO_NOT_EXPOSE_7b3f"


def terminal(item, timestamp=NOW):
    return AttemptFinishedEvent(event_id=item.attempt_id + "/finished", run_id=item.run_id,
                                episode_id=item.episode_id, timestamp=timestamp, payload=item)


def task_finished(task_id, episode_id):
    result = run_result()
    execution = result.final_execution.replace(task_id=task_id)
    evaluation = result.final_evaluation.replace(task_id=task_id)
    result = result.replace(task_id=task_id, episode_id=episode_id, final_execution=execution,
        final_evaluation=evaluation, last_evaluated_execution=execution, last_evaluated_evaluation=evaluation)
    return TaskFinishedEvent(event_id=episode_id + "/task-finished", run_id=result.run_id,
                            episode_id=episode_id, timestamp=NOW, payload=result)


class ContextTests(unittest.TestCase):
    def test_private_feedback_metadata_trace_and_artifact_refs_are_filtered(self):
        agent, stable, artifacts, task, record, live, policy = fixture()
        metadata = task.model_dump(mode="json")["public_metadata"]
        metadata["private_label"] = CANARY
        task = task.replace(public_metadata=metadata)
        raw = stable.model_dump(mode="json")
        raw["config"]["private_label"] = CANARY
        raw["artifacts"]["private_evaluation"] = artifacts.put(CANARY.encode(), "evaluation_spec").model_dump(mode="json")
        stable = stable.model_validate(raw)
        events = [
            TraceEvent(seq=0, kind="model_request", payload={"request": {"messages": [{"content": CANARY}]}}),
            TraceEvent(seq=1, kind="model_response", payload={"content": '{"answer":74}', "metadata": {"private_label": CANARY},
                "usage": {"input_tokens": 5, "private_label": CANARY},
                "tool_calls": [{"call_id": "a", "name": "calculator", "arguments": {"expression": "1+1", "private_label": CANARY}}]}),
            TraceEvent(seq=2, kind="tool_result", payload={"name": "calculator", "status": "error",
                "output": {"value": 2, "private_label": CANARY}, "error": {"message": CANARY}, "private_label": CANARY}),
            TraceEvent(seq=3, kind="output_parse_error", payload={"message": CANARY}),
            TraceEvent(seq=4, kind="unknown_private_event", payload={"private_label": CANARY}),
        ]
        execution = record.execution.replace(trajectory=events, output={"answer": 74, "private_label": CANARY},
                                             state_summary={"private_label": CANARY, "model_steps": 1})
        record = record.replace(execution=execution, evaluation=record.evaluation.replace(feedback=CANARY),
            feedback=record.feedback.replace(public_feedback=CANARY),
            diagnosis=record.diagnosis.replace(explanation=CANARY, evidence_step_ids=[4]))
        state = ContextBuilder(MemoryExperience(), policy).build(task, record, stable, live, round_id=1, feasible_targets=["prompt"])
        encoded = state.model_dump_json()
        self.assertNotIn(CANARY, encoded)
        self.assertNotIn("private_evaluation", encoded)
        self.assertNotIn("eval_spec_ref", encoded)
        self.assertEqual(state.diagnosis.evidence_step_ids, ())
        self.assertEqual([event.seq for event in state.execution.trajectory], [0, 1, 2, 3])
        self.assertEqual(state.execution.trajectory[2].payload["output"], {"value": 2})
        self.assertIsNone(state.remaining_budget)
        self.assertEqual(state.stable_snapshot.version_id, stable.version_id)

    def test_unknown_reason_uses_public_template(self):
        _, stable, _, task, record, live, policy = fixture()
        record = record.replace(evaluation=record.evaluation.replace(reason_code="private_reason", feedback=CANARY))
        state = ContextBuilder(MemoryExperience(), policy).build(task, record, stable, live, round_id=1, feasible_targets=["prompt"])
        self.assertEqual(state.feedback.reason_code, "unknown_failure")
        self.assertNotIn(CANARY, state.model_dump_json())

    def test_test_tasks_wrong_versions_and_wrong_scopes_are_blocked(self):
        _, stable, _, task, record, live, policy = fixture()
        builder = ContextBuilder(MemoryExperience(), policy)
        with self.assertRaises(ContractError):
            ContextBuilder(MemoryExperience(), policy.replace(allow_evolution=False)).build(
                task, record, stable, live, round_id=1, feasible_targets=["prompt"])
        cases = [(task, stable.replace(version_id="other"), live),
                 (task, stable, type(live).from_record(live.to_record().replace(episode_id="other"), budget=None))]
        for public, snapshot, context in cases:
            with self.subTest(task=public), self.assertRaises(ContractError):
                builder.build(public, record, snapshot, context, round_id=1, feasible_targets=["prompt"])
        with self.assertRaises(NoFeasibleTargets):
            builder.build(task, record, stable, live, round_id=1, feasible_targets=[])

    def test_independent_history_excludes_other_tasks_episodes_rounds_and_future(self):
        _, stable, _, task, record, live, policy = fixture()
        events = [terminal(attempt(attempt_id="old")),
                  terminal(attempt(attempt_id="other-episode").replace(episode_id="other")),
                  terminal(attempt(attempt_id="other-task").replace(task_id="dev-002")),
                  terminal(attempt(attempt_id="same-round").replace(round_id=2)),
                  terminal(attempt(attempt_id="future"), NOW + timedelta(seconds=1))]
        state = ContextBuilder(MemoryExperience(events), policy).build(task, record, stable, live,
            round_id=2, feasible_targets=["prompt"], as_of=NOW, allowed_history_task_ids=["dev-002"])
        self.assertEqual([item.attempt_id for item in state.history], ["old"])

    def test_continual_history_requires_finished_development_tasks(self):
        _, stable, _, task, record, live, policy = fixture()
        past = attempt(attempt_id="past").replace(episode_id="past", task_id="dev-002")
        unfinished = attempt(attempt_id="unfinished").replace(episode_id="unfinished", task_id="dev-003")
        testing = attempt(attempt_id="test").replace(episode_id="testing", task_id="test-001")
        events = [terminal(past), task_finished("dev-002", "past"), terminal(unfinished),
                  terminal(testing), task_finished("test-001", "testing")]
        state = ContextBuilder(MemoryExperience(events), policy).build(task, record, stable, live,
            round_id=2, feasible_targets=["prompt"], as_of=NOW, task_mode="continual",
            allowed_history_task_ids=["dev-002", "dev-003"])
        self.assertEqual([item.attempt_id for item in state.history], ["past"])

    def test_failed_empty_and_rejected_history_retains_unknown_and_actual_rewards(self):
        _, stable, _, task, record, live, policy = fixture()
        rejected = attempt(attempt_id="rejected").replace(decision=EvolutionDecision(target="prompt", rationale="Use the public failure evidence"),
            diagnosis=record.diagnosis.replace(explanation=CANARY))
        empty = attempt(status="empty_candidates", candidate_ids=[], attempt_id="empty")
        policy_result = acceptance(False).replace(reason=CANARY)
        result = CandidateEvaluation(attempt_id="rejected", candidate_id="candidate-1", candidate_version_id="v2",
            validation_ref=None, acceptance=policy_result, status="rejected", selected=False, committed=False,
            commit_error=None, final_recorded_at=NOW)
        events = [terminal(rejected), terminal(empty), CandidateEvaluatedEvent(event_id="outcome", run_id="run-1",
            episode_id="episode-1", timestamp=NOW, payload=result)]
        state = ContextBuilder(MemoryExperience(events), policy).build(task, record, stable, live, round_id=2,
            feasible_targets=["prompt"], as_of=NOW)
        self.assertNotIn(CANARY, state.model_dump_json())
        self.assertEqual(len(state.history), 2)
        summary = next(item for item in state.history if item.attempt_id == "rejected")
        self.assertEqual(summary.diagnosis.reason_code, "incorrect_answer")
        self.assertEqual(summary.diagnosis.evidence_step_ids, ())
        self.assertEqual(summary.candidate_summaries[0].reward, 0.5)
        self.assertFalse(summary.candidate_summaries[0].accepted)
        self.assertEqual(summary.candidate_summaries[0].acceptance_reason, "unknown")
        self.assertEqual(next(item for item in state.history if item.attempt_id == "empty").candidate_summaries, ())
        self.assertEqual(summary.stable_version_after, summary.stable_version_id)
        self.assertEqual(summary.decision.rationale, "Use the public failure evidence")
        self.assertEqual(next(item for item in state.history if item.attempt_id == "empty").attempt_status, "empty_candidates")

    def test_history_includes_candidate_change_and_policy_reason_with_configured_limits(self):
        _, stable, _, task, record, live, policy = fixture()
        first = attempt(attempt_id="first")
        second = attempt(attempt_id="second").replace(
            candidate_changes={"candidate-1": CandidateChange(target="model", updated_components=["model"],
                change_summary="Execution model: gpt-5.6-sol -> gpt-6.1-sol",
                generation_rationale="Try a stronger model after repeated tool failures")})
        result = CandidateEvaluation(attempt_id="second", candidate_id="candidate-1", candidate_version_id="v2",
            validation_ref=None, acceptance=acceptance(False), status="rejected", selected=False,
            committed=False, commit_error=None, final_recorded_at=NOW)
        state = ContextBuilder(MemoryExperience([terminal(first), terminal(second),
            CandidateEvaluatedEvent(event_id="second/outcome", run_id="run-1", episode_id="episode-1",
                timestamp=NOW, payload=result)]),
            policy.replace(history_max_attempts=1, history_max_text_chars=35)).build(
                task, record, stable, live, round_id=2, feasible_targets=["prompt"], as_of=NOW)
        self.assertEqual([item.attempt_id for item in state.history], ["second"])
        candidate = state.history[0].candidate_summaries[0]
        self.assertEqual(candidate.target, "model")
        self.assertEqual(candidate.updated_components, ("model",))
        self.assertEqual(candidate.change_summary, "Execution model: gpt-5.6-sol -> gpt")
        self.assertEqual(candidate.generation_rationale, "Try a stronger model after repeated")
        self.assertEqual(candidate.acceptance_reason, "regression_guard")
        self.assertTrue(candidate.change_summary_truncated)
        self.assertTrue(candidate.generation_rationale_truncated)

    def test_actual_changes_have_independent_limits_and_exclude_private_components(self):
        _, stable, _, task, record, live, policy = fixture()
        instruction = "Keep caller contracts. " * 25 + "Catch at the outermost CLI boundary."
        previous = attempt().replace(candidate_changes={"candidate-1": CandidateChange(target="prompt",
            updated_components=["prompt", "evaluation_spec"], generation_rationale="A" * 50,
            actual_changes=[ComponentEdit(component="prompt", operation="insert", before="", after=instruction),
                            ComponentEdit(component="evaluation_spec", operation="insert", before="", after=CANARY)])})
        builder = ContextBuilder(MemoryExperience([terminal(previous)]),
            policy.replace(history_max_text_chars=20, history_max_change_chars=1000))
        state = builder.build(task, record, stable, live, round_id=2, feasible_targets=["prompt"], as_of=NOW)
        candidate = state.history[0].candidate_summaries[0]
        self.assertEqual(candidate.actual_changes[0].after, instruction)
        self.assertFalse(candidate.changes_truncated)
        self.assertTrue(candidate.generation_rationale_truncated)
        self.assertNotIn(CANARY, state.model_dump_json())
        limited = ContextBuilder(builder.experience, builder.policy.replace(history_max_change_chars=90)).build(
            task, record, stable, live, round_id=2, feasible_targets=["prompt"], as_of=NOW)
        clipped = limited.history[0].candidate_summaries[0]
        self.assertEqual(clipped.actual_changes[0].after, instruction[:90])
        self.assertTrue(clipped.actual_changes[0].truncated)
        self.assertTrue(clipped.changes_truncated)
