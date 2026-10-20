import json
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from experiments.execution import prepare_execution
from experiments.evolution import assemble_evolution
from experiments.validation import assemble_validation
from experiments.reporting import summarize_saved_run
from tests.orchestration.helpers import attach_model, configured
from tests.execution.helpers import RecordingModel, response


class SingleObservationTests(unittest.TestCase):
    def test_each_version_task_is_executed_and_graded_once_across_commit_and_next_round(self):
        with tempfile.TemporaryDirectory() as directory:
            _, startup = configured(Path(directory), rounds=2)
            task = startup.prepare_task("dev-001")
            model, _ = attach_model(task, local_failure=True)
            agent, evaluator = task.execution.agent, task.execution.benchmark.evaluator
            with patch.object(agent, "run", wraps=agent.run) as runs, patch.object(evaluator, "evaluate", wraps=evaluator.evaluate) as scores:
                result = task.run()
            self.assertEqual(result.stop_reason, "max_evolution_rounds_reached")
            observations = [(call.args[0].task_id, call.args[1].version_id, call.args[2].seed) for call in runs.call_args_list]
            self.assertEqual(len(observations), len(set(observations)))
            self.assertEqual(scores.call_count, runs.call_count)
            self.assertFalse(any(ctx.stage == "post_commit_execution" for _, ctx in model.requests))
            root = startup.execution.storage.run_root
            reports = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(root.glob("rounds/*/round-*/record.json"))]
            first, second = reports
            self.assertTrue(first["candidates"][0]["committed"])
            previous = first["candidates"][0]["validation"]
            current = second["candidates"][0]["validation"]
            self.assertEqual(previous["candidate_metrics"]["score"], current["baseline_metrics"]["score"])
            for a, b in zip(previous["tasks"], current["tasks"]):
                self.assertEqual(a["candidate"]["evaluation"], b["baseline"]["evaluation"])
                self.assertEqual(a["candidate"]["execution"], b["baseline"]["execution"])
                self.assertTrue(b["baseline_cached"])
                self.assertEqual(b["baseline"]["new_usage"]["model_calls"], 0)
            self.assertTrue(first["post_commit_result"]["reused"])
            self.assertEqual(first["post_commit_result"]["new_usage"]["model_calls"], 0)
            self.assertEqual(first["post_commit_result"]["source"], "candidate_validation")
            self.assertNotIn("post_commit_execution", first)
            text = (root / "evolution.txt").read_text(encoding="utf-8")
            self.assertIn("提交后任务结果（复用候选验证）", text)
            self.assertNotIn("报告异常", text)
            self.assertNotIn("提交后复测", text)

    def test_standalone_execution_and_later_validation_share_the_same_observation(self):
        with tempfile.TemporaryDirectory() as directory:
            path, _ = configured(Path(directory), rounds=1)
            execution = prepare_execution(path, run_id="standalone")
            agent, evaluator = execution.agent, execution.benchmark.evaluator
            with patch.object(agent, "run", wraps=agent.run) as runs, patch.object(evaluator, "evaluate", wraps=evaluator.evaluate) as scores:
                first = execution.run_task("dev-001")
                second = execution.run_task("dev-001")
                self.assertEqual(runs.call_count, 1)
                self.assertEqual(scores.call_count, 1)
                self.assertEqual(first.execution, second.execution)
                self.assertTrue(second.execution_reused)
                self.assertEqual(execution.save_record(second)["usage"]["total_tokens"], 0)
                evolution = assemble_evolution(execution)
                proposal = evolution.propose(second, round_id=1)
                batch = assemble_validation(execution).validate(proposal)
            candidate = next(iter(batch.current_task_results.values()))
            self.assertNotEqual(candidate.version_id, first.execution.agent_version_id)
            observations = [(call.args[0].task_id, call.args[1].version_id, call.args[2].seed) for call in runs.call_args_list]
            self.assertEqual(len(observations), len(set(observations)))
            self.assertEqual(scores.call_count, runs.call_count)

    def test_selected_candidate_without_current_task_evidence_cannot_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            _, startup = configured(Path(directory), rounds=1)
            task = startup.prepare_task("dev-001")
            attach_model(task)
            validate = task.validation.validate
            task.controller.validation = SimpleNamespace(validate=lambda proposal: validate(proposal).replace(current_task_results={}))
            result = task.run()
            self.assertEqual(result.stop_reason, "validation_error")
            self.assertEqual(result.initial_version_id, result.final_stable_version_id)
            self.assertFalse(any(event.record_type == "version_committed" for event in task.execution.storage.experience_store.events()))

    def test_incomplete_initial_usage_stops_without_reexecuting_current_task(self):
        with tempfile.TemporaryDirectory() as directory:
            _, startup = configured(Path(directory), rounds=1)
            task = startup.prepare_task("dev-001")
            model = RecordingModel(lambda request, ctx: response('{"answer":-999}', unknown=True))
            task.execution.agent.model = model
            result = task.run()
            self.assertEqual(result.stop_reason, "execution_error")
            self.assertEqual(len(model.requests), 1)
            self.assertEqual(result.evolution_rounds_used, 0)
            self.assertIsNone(result.usage.total_tokens)
            self.assertEqual(result.final_evaluation.score, 0)

    def test_cached_initial_result_keeps_episode_ownership_during_offline_export(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, startup = configured(root, rounds=1)
            first = startup.prepare_task("dev-001")
            attach_model(first, initial_success=True)
            result1 = first.run()
            second = startup.prepare_task("dev-001")
            model, _ = attach_model(second, always_wrong=True)
            result2 = second.run()
            self.assertEqual(result1.final_execution, result2.final_execution)
            self.assertEqual(result2.stop_reason, "task_succeeded")
            self.assertEqual(result2.usage.model_calls, 0)
            self.assertFalse(model.requests)
            self.assertNotEqual(result1.episode_id, result2.episode_id)
            output = root / "result.json"
            output.write_text(json.dumps(startup.save_record(result2)), encoding="utf-8")
            report = startup.execution.storage.run_root / "evolution.txt"
            report.unlink()
            summarize_saved_run(output)
            text = report.read_text(encoding="utf-8")
            self.assertEqual(text.count("【初始执行】"), 2)
            self.assertEqual(text.count("[任务记录结束："), 2)
            self.assertIn("本次新增成本：tokens=0", text)
