import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aer_core.errors import CommitError, ContractError, DiagnosisError, EvaluationError, RoutingError
from aer_core.schemas.experience import AttemptStartedEvent, CandidateEvaluatedEvent, TaskFinishedEvent, VersionCommittedEvent
from aer_core.schemas.experiment import RunResult
from tests.orchestration.helpers import attach_model, configured


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def setup_task(self, **kwargs):
        _, startup = configured(self.root, **kwargs)
        return startup, startup.prepare_task("dev-001")

    def events(self, task, kind):
        return [event for event in task.execution.storage.experience_store.events()
                if isinstance(event, kind) and event.episode_id == task.context.episode_id]

    def test_initial_success_skips_diagnosis_and_evolution(self):
        startup, task = self.setup_task()
        model, _ = attach_model(task, initial_success=True)
        with patch.object(task.controller.diagnoser, "diagnose", side_effect=AssertionError("must not diagnose")):
            result = task.run()
        self.assertEqual(result.stop_reason, "task_succeeded")
        self.assertEqual(result.evolution_rounds_used, 0)
        self.assertEqual(len(model.requests), 1)
        self.assertEqual(len(self.events(task, TaskFinishedEvent)), 1)
        self.assertEqual(startup.save_record(result)["status"], "task_finished")
        self.assertEqual(RunResult.model_validate_json(result.model_dump_json()), result)
        with self.assertRaises(ContractError):
            task.run()

    def test_disabled_failure_skips_diagnosis_and_preserves_score(self):
        _, task = self.setup_task(mode="disabled")
        attach_model(task)
        with patch.object(task.controller.diagnoser, "diagnose", side_effect=AssertionError("must not diagnose")):
            result = task.run()
        self.assertEqual(result.stop_reason, "evolution_disabled")
        self.assertEqual(result.final_evaluation_status, "evaluated")
        self.assertFalse(result.final_evaluation.success)
        self.assertEqual(result.evolution_rounds_used, 0)

    def test_initial_provider_failure_stops_without_diagnosis_or_attempt(self):
        _, task = self.setup_task()
        model, _ = attach_model(task)
        with patch.object(model, "generate", side_effect=RuntimeError("fixture initial provider failure")):
            result = task.run()
        self.assertEqual(result.stop_reason, "execution_error")
        self.assertEqual(result.stop_stage, "initial_execution")
        self.assertEqual(result.evolution_rounds_used, 0)
        self.assertEqual(result.final_evaluation_status, "not_evaluated")
        self.assertEqual(result.usage.model_calls, 1)
        self.assertIsNone(result.usage.total_tokens)

    def test_fixed_zero_rounds_and_no_targets_stop_before_diagnosis(self):
        _, task = self.setup_task(rounds=0)
        attach_model(task)
        with patch.object(task.controller.diagnoser, "diagnose", side_effect=AssertionError("must not diagnose")):
            result = task.run()
        self.assertEqual(result.stop_reason, "max_evolution_rounds_reached")
        self.assertEqual(result.evolution_rounds_used, 0)

    def test_no_feasible_target_occupies_no_round(self):
        _, task = self.setup_task()
        attach_model(task)
        with patch.object(type(task.evolution), "feasible_targets", return_value=()):
            result = task.run()
        self.assertEqual(result.stop_reason, "no_feasible_targets")
        self.assertEqual(result.evolution_rounds_used, 0)

    def test_empty_candidates_consume_rounds_reuse_execution_and_diagnosis(self):
        _, task = self.setup_task(rounds=3)
        model, _ = attach_model(task, empty=True)
        diagnose = task.controller.diagnoser.diagnose
        with patch.object(task.controller.diagnoser, "diagnose", wraps=diagnose) as recorder:
            result = task.run()
        self.assertEqual(result.stop_reason, "max_evolution_rounds_reached")
        self.assertEqual(result.evolution_rounds_used, 3)
        self.assertEqual(recorder.call_count, 1)
        self.assertEqual(len(model.requests), 4)
        self.assertEqual([event.payload.round_id for event in self.events(task, AttemptStartedEvent)], [1, 2, 3])
        generations = [req for req, _ in model.requests if '"prompt_evolution"' in req.messages[-1].content]
        import json
        self.assertEqual([len(json.loads(req.messages[-1].content)["context"]["history"]) for req in generations], [0, 1, 2])

    def test_manual_plan_exhaustion_does_not_reuse_last_decision(self):
        _, task = self.setup_task(mode="manual", rounds=3)
        attach_model(task, empty=True)
        result = task.run()
        self.assertEqual(result.stop_reason, "manual_plan_exhausted")
        self.assertEqual(result.evolution_rounds_used, 1)

    def test_both_agents_commit_exact_validated_version_and_reuse_current_result(self):
        for single in (False, True):
            with self.subTest(single=single), tempfile.TemporaryDirectory() as root:
                _, startup = configured(Path(root), rounds=1, single=single)
                task = startup.prepare_task("dev-001")
                model, reports = attach_model(task)
                result = task.run()
                self.assertEqual(result.stop_reason, "task_succeeded")
                self.assertEqual(result.evolution_rounds_used, 1)
                self.assertNotEqual(result.initial_version_id, result.final_stable_version_id)
                self.assertEqual(task.execution.storage.version_store.stable().version_id, result.final_stable_version_id)
                commits = self.events(task, VersionCommittedEvent)
                self.assertEqual(len(commits), 1)
                self.assertEqual(commits[0].payload.committed_version_id, result.final_execution.agent_version_id)
                self.assertEqual(result.usage.model_calls, len(model.requests))
                self.assertEqual(result.usage.total_tokens, sum(item.input_tokens + item.output_tokens for item in reports))
                self.assertEqual({ctx.episode_id for _, ctx in model.requests}, {result.episode_id})
                self.assertEqual(sum(ctx.stage == "post_commit_execution" for _, ctx in model.requests), 0)
                self.assertIn("/candidate/", result.final_execution.operation_id)
                outcome = self.events(task, CandidateEvaluatedEvent)[-1].payload
                self.assertTrue(outcome.committed)
                self.assertEqual(result.final_evaluation_status, "evaluated")

    def test_ordinary_rejections_continue_without_committing_or_reexecuting_current(self):
        _, task = self.setup_task(rounds=2)
        model, _ = attach_model(task, always_wrong=True)
        result = task.run()
        self.assertEqual(result.stop_reason, "max_evolution_rounds_reached")
        self.assertEqual(result.evolution_rounds_used, 2)
        self.assertEqual(result.final_stable_version_id, result.initial_version_id)
        self.assertEqual(sum(ctx.stage == "initial_execution" for _, ctx in model.requests), 1)
        self.assertFalse(self.events(task, VersionCommittedEvent))
        # Second batch reuses the full baseline; all actual model calls counted once.
        self.assertEqual(result.usage.model_calls, len(model.requests))
        self.assertEqual(len(model.requests), 29)

    def test_failed_local_result_keeps_improved_committed_version(self):
        _, task = self.setup_task(rounds=1)
        attach_model(task, local_failure=True)
        result = task.run()
        self.assertEqual(result.stop_reason, "max_evolution_rounds_reached")
        self.assertFalse(result.final_evaluation.success)
        self.assertNotEqual(result.final_stable_version_id, result.initial_version_id)
        self.assertEqual(result.final_execution.agent_version_id, result.final_stable_version_id)
        self.assertIn("/candidate/", result.final_execution.operation_id)

    def test_next_round_uses_committed_snapshot_and_saved_local_result(self):
        import json
        _, task = self.setup_task(rounds=2)
        model, _ = attach_model(task, local_failure=True)
        result = task.run()
        self.assertEqual(result.stop_reason, "max_evolution_rounds_reached")
        self.assertEqual(result.evolution_rounds_used, 2)
        payloads = [json.loads(req.messages[-1].content) for req, _ in model.requests
                    if '"prompt_evolution"' in req.messages[-1].content]
        self.assertEqual(payloads[0]["context"]["stable_version_id"], result.initial_version_id)
        self.assertEqual(payloads[1]["context"]["stable_version_id"], result.final_stable_version_id)
        second = payloads[1]["context"]["history"][0]["candidate_summaries"][0]
        self.assertTrue(second["committed"])
        self.assertIn("IMPROVED reusable instructions", second["change_summary"])
        self.assertEqual(second["acceptance_reason"], "improved")
        history = payloads[1]["context"]["history"][0]
        self.assertEqual(history["stable_version_id"], result.initial_version_id)
        self.assertEqual(history["stable_version_after"], result.final_stable_version_id)
        self.assertEqual(second["validation"]["independent"]["after"], 1)
        self.assertEqual(second["validation"]["current_task"]["after"], 0)
        self.assertEqual(payloads[1]["routing_decision"]["target"], "prompt")
        self.assertEqual(sum(ctx.stage == "post_commit_execution" for _, ctx in model.requests), 0)
        self.assertEqual(result.usage.model_calls, len(model.requests))

    def test_rejected_first_round_can_accept_second_using_cached_baseline(self):
        import json
        _, task = self.setup_task(rounds=2)
        model, _ = attach_model(task)
        original = model.make_response
        def answer(request, context):
            result = original(request, context)
            payload = json.loads(request.messages[-1].content)
            if payload.get("operation") == "prompt_evolution" and payload["context"]["round_id"] == 1:
                return result.replace(content='{"candidates":[{"prompt":"Unhelpful reusable instructions."}]}')
            return result
        model.make_response = answer
        result = task.run()
        self.assertEqual(result.stop_reason, "task_succeeded")
        self.assertEqual(result.evolution_rounds_used, 2)
        self.assertEqual(len(model.requests), 29)
        self.assertEqual(result.usage.model_calls, 29)
        self.assertEqual(len(self.events(task, VersionCommittedEvent)), 1)

    def test_post_commit_adoption_does_not_call_agent_again(self):
        _, task = self.setup_task(rounds=1)
        attach_model(task, post_commit_error=True)
        result = task.run()
        self.assertEqual(result.stop_reason, "task_succeeded")
        self.assertEqual(result.stop_stage, "commit")
        self.assertEqual(result.final_evaluation_status, "evaluated")
        self.assertEqual(result.final_execution.agent_version_id, result.final_stable_version_id)
        self.assertTrue(result.final_evaluation.success)
        self.assertIn("/candidate/", result.final_execution.operation_id)
        self.assertNotEqual(result.final_stable_version_id, result.initial_version_id)
        self.assertIsNotNone(result.usage.total_tokens)

    def test_post_commit_adoption_does_not_call_evaluator_again(self):
        _, task = self.setup_task(rounds=1)
        attach_model(task)
        evaluator = task.execution.benchmark.evaluator
        original = evaluator.evaluate
        def evaluate(task, execution, spec, ctx):
            if "post_commit_execution" in execution.operation_id:
                raise EvaluationError("fixture scoring failure", stage="evaluation")
            return original(task, execution, spec, ctx)
        with patch.object(evaluator, "evaluate", side_effect=evaluate):
            result = task.run()
        self.assertEqual(result.stop_reason, "task_succeeded")
        self.assertEqual(result.final_evaluation_status, "evaluated")
        self.assertEqual(result.final_stable_version_id, result.last_evaluated_execution.agent_version_id)

    def test_diagnosis_and_router_errors_are_terminal_with_correct_round_count(self):
        _, task = self.setup_task()
        attach_model(task)
        with patch.object(task.controller.diagnoser, "diagnose", side_effect=DiagnosisError("fixture failure", stage="diagnosis")):
            result = task.run()
        self.assertEqual(result.stop_reason, "diagnosis_error")
        self.assertEqual(result.evolution_rounds_used, 0)

    def test_router_failure_consumes_started_round_and_records_experience(self):
        _, task = self.setup_task()
        attach_model(task)
        with patch.object(task.evolution.router, "select", side_effect=RoutingError("fixture failure", stage="routing")):
            result = task.run()
        self.assertEqual(result.stop_reason, "routing_error")
        self.assertEqual(result.evolution_rounds_used, 1)
        self.assertEqual(len(self.events(task, TaskFinishedEvent)), 1)

    def test_generation_failure_stops_with_cost_and_attempt(self):
        _, task = self.setup_task()
        attach_model(task, generation_error=True)
        result = task.run()
        self.assertEqual(result.stop_reason, "evolution_error")
        self.assertEqual(result.evolution_rounds_used, 1)
        self.assertEqual(result.usage.model_calls, 2)
        self.assertIsNone(result.usage.total_tokens)

    def test_incomplete_validation_stops_without_committing(self):
        _, task = self.setup_task()
        attach_model(task, unknown_validation=True)
        result = task.run()
        self.assertEqual(result.stop_reason, "validation_error")
        self.assertEqual(result.evolution_rounds_used, 1)
        self.assertEqual(result.final_stable_version_id, result.initial_version_id)
        self.assertIsNone(result.usage.total_tokens)

    def test_one_incomplete_candidate_prevents_commit_of_another_accepted_candidate(self):
        _, task = self.setup_task(candidates=2)
        attach_model(task, candidates=2, mixed=True)
        result = task.run()
        self.assertEqual(result.stop_reason, "validation_error")
        self.assertFalse(self.events(task, VersionCommittedEvent))
        self.assertTrue(any(event.payload.acceptance and event.payload.acceptance.accepted
                            for event in self.events(task, CandidateEvaluatedEvent)))

    def test_commit_failure_keeps_old_stable_and_records_real_commit_outcome(self):
        _, task = self.setup_task()
        attach_model(task)
        versions = task.execution.storage.version_store
        with patch.object(versions, "commit", side_effect=CommitError("fixture failure", stage="commit")):
            result = task.run()
        self.assertEqual(result.stop_reason, "commit_error")
        self.assertEqual(result.final_stable_version_id, result.initial_version_id)
        outcome = self.events(task, CandidateEvaluatedEvent)[-1].payload
        self.assertEqual(outcome.status, "commit_failed")
        self.assertTrue(outcome.selected)
        self.assertFalse(outcome.committed)

    def test_commit_publication_followed_by_log_failure_recovers_and_adopts_saved_result(self):
        _, task = self.setup_task(rounds=1)
        model, _ = attach_model(task)
        versions = task.execution.storage.version_store
        finalize = versions._finalize
        count = 0
        def interrupted(journal):
            nonlocal count
            count += 1
            if count == 1:
                raise OSError("fixture receipt failure after publication")
            return finalize(journal)
        with patch.object(versions, "_finalize", side_effect=interrupted):
            result = task.run()
        self.assertEqual(result.stop_reason, "task_succeeded")
        self.assertGreaterEqual(count, 2)
        self.assertEqual(len(self.events(task, VersionCommittedEvent)), 1)
        self.assertEqual(sum(ctx.stage == "post_commit_execution" for _, ctx in model.requests), 0)

    def test_persistent_commit_receipt_error_preserves_published_pointer_and_actual_status(self):
        _, task = self.setup_task(rounds=1)
        attach_model(task)
        versions = task.execution.storage.version_store
        with patch.object(versions, "_finalize", side_effect=OSError("fixture persistent receipt failure")):
            result = task.run()
        self.assertEqual(result.stop_reason, "commit_error")
        self.assertNotEqual(result.final_stable_version_id, result.initial_version_id)
        self.assertEqual(result.final_evaluation_status, "evaluated")
        self.assertEqual(result.final_execution.agent_version_id, result.final_stable_version_id)
        self.assertTrue(self.events(task, CandidateEvaluatedEvent)[-1].payload.committed)
        # Reopening the durable pointer completes its receipt after the injected failure ends.
        self.assertEqual(versions.stable().version_id, result.final_stable_version_id)
