import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aer_core.errors import EvaluationError
from aer_core.schemas.experience import AttemptStartedEvent, CandidateEvaluatedEvent, TaskFinishedEvent
from plugins.benchmarks.toy_tasks import ToyBenchmark
from tests.orchestration.helpers import ROOT, attach_model, configured


class StartupTests(unittest.TestCase):
    def test_independent_tasks_use_initial_version_and_distinct_pointer_scopes(self):
        with tempfile.TemporaryDirectory() as directory:
            _, startup = configured(Path(directory), rounds=1)
            first = startup.prepare_task("dev-001")
            attach_model(first)
            result1 = first.run()
            second = startup.prepare_task("dev-002")
            self.assertNotEqual(first.scope_id, second.scope_id)
            self.assertEqual(second.execution.storage.version_store.stable().version_id, result1.initial_version_id)
            attach_model(second)
            result2 = second.run()
            self.assertEqual(result2.initial_version_id, result1.initial_version_id)
            self.assertNotEqual(result1.episode_id, result2.episode_id)
            self.assertEqual(result2.evolution_rounds_used, 1)
            self.assertEqual(first.execution.storage.version_store.stable().version_id, result1.final_stable_version_id)
            self.assertEqual(startup.execution.storage.version_store.stable().version_id, result1.initial_version_id)

    def test_continual_tasks_keep_committed_version_but_reset_rounds_and_session(self):
        with tempfile.TemporaryDirectory() as directory:
            _, startup = configured(Path(directory), rounds=1, task_mode="continual")
            first = startup.prepare_task("dev-001")
            attach_model(first)
            result1 = first.run()
            second = startup.prepare_task("dev-002")
            attach_model(second)
            result2 = second.run()
            self.assertEqual(first.scope_id, second.scope_id)
            self.assertEqual(result2.initial_version_id, result1.final_stable_version_id)
            self.assertEqual(result2.evolution_rounds_used, 0)
            self.assertEqual(result2.stop_reason, "task_succeeded")
            self.assertTrue(result2.final_execution.state_summary["fresh_session"])
            self.assertNotEqual(result2.episode_id, result1.episode_id)

    def test_continual_history_contains_completed_prior_task_with_real_commit_status(self):
        with tempfile.TemporaryDirectory() as directory:
            _, startup = configured(Path(directory), rounds=1, task_mode="continual")
            first = startup.prepare_task("dev-001")
            attach_model(first, local_failure=True)
            result1 = first.run()
            second = startup.prepare_task("dev-002")
            model, _ = attach_model(second, local_failure=True, empty=True)
            result2 = second.run()
            contexts = [json.loads(req.messages[-1].content)["context"] for req, _ in model.requests
                        if '"prompt_evolution"' in req.messages[-1].content]
            self.assertEqual(len(contexts), 1)
            self.assertEqual(contexts[0]["history"][0]["task_id"], "dev-001")
            self.assertTrue(contexts[0]["history"][0]["candidate_summaries"][0]["committed"])
            self.assertEqual(result2.evolution_rounds_used, 1)
            self.assertEqual(result2.initial_version_id, result1.final_stable_version_id)

    def test_diagnoser_receives_filtered_views_not_private_evaluator_feedback(self):
        with tempfile.TemporaryDirectory() as directory:
            _, startup = configured(Path(directory), rounds=1)
            task = startup.prepare_task("dev-001")
            attach_model(task, empty=True)
            canary = "PRIVATE_EVALUATOR_FEEDBACK_ace47"
            evaluator = task.execution.benchmark.evaluator
            original = evaluator.evaluate
            diagnosis = task.controller.diagnoser.diagnose
            seen = []
            def diagnose(public, execution, feedback, ctx):
                seen.extend([public.model_dump_json(), execution.model_dump_json(), feedback.model_dump_json()])
                return diagnosis(public, execution, feedback, ctx)
            with patch.object(evaluator, "evaluate", side_effect=lambda *args: original(*args).replace(feedback=canary)), \
                 patch.object(task.controller.diagnoser, "diagnose", side_effect=diagnose):
                result = task.run()
            self.assertEqual(result.stop_reason, "max_evolution_rounds_reached")
            self.assertTrue(seen)
            self.assertTrue(all(canary not in text for text in seen))
            self.assertNotIn('"request":', seen[1])

    def test_initial_scoring_error_has_no_attempt_and_no_fabricated_final_score(self):
        with tempfile.TemporaryDirectory() as directory:
            _, startup = configured(Path(directory))
            task = startup.prepare_task("dev-001")
            attach_model(task)
            with patch.object(task.execution.benchmark.evaluator, "evaluate", side_effect=EvaluationError("fixture failure", stage="evaluation")):
                result = task.run()
            self.assertEqual(result.stop_reason, "evaluation_error")
            self.assertEqual(result.evolution_rounds_used, 0)
            self.assertEqual(result.final_evaluation_status, "not_evaluated")
            self.assertIsNone(result.last_evaluated_execution)

    def test_final_test_failure_never_diagnoses_or_evolves(self):
        import yaml
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, source = configured(root, mode="disabled")
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            data["benchmark"]["execution_split"] = "test"
            data["agent"]["initial_prompt"] = None
            data["agent"]["initial_snapshot_ref"] = str(source.execution.storage.run_root / source.execution.storage.initial_snapshot_ref.uri)
            path.write_text(yaml.safe_dump(data), encoding="utf-8")
            from experiments.orchestration import prepare_orchestrator
            startup = prepare_orchestrator(path, run_id="test-only")
            task = startup.prepare_task(ToyBenchmark().tasks("test")[0].task_id)
            attach_model(task)
            with patch.object(task.controller.diagnoser, "diagnose", side_effect=AssertionError("must not diagnose test")):
                result = task.run()
            self.assertEqual(result.stop_reason, "evolution_disabled")
            self.assertEqual(result.evolution_rounds_used, 0)
            events = task.execution.storage.experience_store.events()
            self.assertFalse(any(isinstance(event, AttemptStartedEvent) for event in events))

    def test_cli_run_task_other_working_directory_and_output_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, _ = configured(root)
            args = [sys.executable, str(ROOT / "main.py"), "run-task", str(path), "--task-id", "dev-001", "--run-id", "cli", "--json"]
            result = subprocess.run(args, cwd=root, encoding="utf-8", capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "task_finished")
            self.assertEqual(payload["record"]["stop_reason"], "max_evolution_rounds_reached")
            self.assertEqual(payload["record"]["evolution_rounds_used"], 2)
            source = path.read_bytes()
            guarded = subprocess.run(args + ["--output", str(path)], cwd=root, encoding="utf-8", capture_output=True)
            self.assertEqual(guarded.returncode, 2)
            self.assertEqual(path.read_bytes(), source)
            managed = root / "runs/cli/managed.json"
            guarded = subprocess.run(args + ["--output", str(managed)], cwd=root, encoding="utf-8", capture_output=True)
            self.assertEqual(guarded.returncode, 2)
            self.assertFalse(managed.exists())
