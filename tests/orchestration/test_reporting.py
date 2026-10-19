import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aer_core.identifiers import timestamp_id
from aer_core.schemas.experience import TaskFinishedEvent
from aer_core.schemas.experiment import RunResult, TaskFinishedSummary
from experiments.reporting import RunReporter, summarize_saved_run
from tests.orchestration.helpers import ROOT, attach_model, configured


class ReportingTests(unittest.TestCase):
    def test_core_log_is_one_file_and_full_result_is_recoverable_from_compact_event(self):
        with tempfile.TemporaryDirectory() as directory:
            _, startup = configured(Path(directory), rounds=1)
            task = startup.prepare_task("dev-001")
            attach_model(task)
            result = task.run()
            root = startup.execution.storage.run_root
            log = (root / "evolution.txt").read_text(encoding="utf-8")
            for stage in ("初始执行", "诊断器输出", "候选 R1-C1", "R1-C1 接受与选择", "提交后复测 1", "最终结果", "成本"):
                self.assertIn(f"【{stage}】", log)
            self.assertIn("独立验证分：0.000 → 1.000", log)
            self.assertNotIn("验证进度", log)
            self.assertNotIn("报告异常", log)
            self.assertNotIn(result.final_stable_version_id, log)
            self.assertNotIn("【原始记录索引】", log)
            self.assertRegex(log, r"执行_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_\d{6} → ")
            execution_section = log.split("【初始执行】")[1].split("\n【", 1)[0]
            self.assertIn(str(root / "rounds"), execution_section)
            self.assertRegex(log, r"上下文_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_\d{6} → ")
            self.assertIn("IMPROVED reusable instructions", log)
            self.assertIn("Evolver 实际收到的反馈", log)
            self.assertNotIn('"request":', log)
            events = task.execution.storage.experience_store.events()
            finished = next(event for event in events if isinstance(event, TaskFinishedEvent))
            self.assertIsInstance(finished.payload, TaskFinishedSummary)
            self.assertNotIn("trajectory", finished.model_dump_json())
            restored = RunResult.model_validate_json(task.execution.storage.artifact_store.get(finished.payload.result_ref))
            self.assertEqual(restored, result)
            second = startup.prepare_task("dev-002")
            attach_model(second, initial_success=True)
            second.run()
            self.assertEqual(len(list(root.rglob("evolution.txt"))), 1)
            self.assertEqual(len(list(root.rglob("*.log"))), 0)
            self.assertIn("dev-002", (root / "evolution.txt").read_text(encoding="utf-8"))

    def test_rejection_and_incomplete_validation_keep_clear_terminal_reason(self):
        for model_options, expected in (({"always_wrong": True}, "rejected"), ({"unknown_validation": True}, "validation_error")):
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as directory:
                _, startup = configured(Path(directory), rounds=1)
                task = startup.prepare_task("dev-001")
                attach_model(task, **model_options)
                result = task.run()
                log = (startup.execution.storage.run_root / "evolution.txt").read_text(encoding="utf-8")
                self.assertIn(expected, log)
                self.assertIn("稳定版本未更新", log)
                self.assertEqual(result.initial_version_id, result.final_stable_version_id)

    def test_timestamp_identity_is_readable_and_distinct(self):
        first, second = timestamp_id("run"), timestamp_id("run")
        self.assertRegex(first, r"^run-\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_\d{6}$")
        self.assertNotEqual(first, second)

    def test_console_details_are_not_written_and_log_failure_does_not_change_result(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stream = io.StringIO()
            reporter = RunReporter(root, console=True, stream=stream)
            reporter.emit("验证进度", "同类任务执行中", detail=True)
            self.assertFalse(reporter.path.exists())
            reporter.emit("验证", "验证开始")
            self.assertIn("验证进度", stream.getvalue())
            self.assertNotIn("验证进度", reporter.path.read_text(encoding="utf-8"))
            _, startup = configured(root, rounds=1)
            task = startup.prepare_task("dev-001")
            attach_model(task)
            with patch.object(task.controller.progress, "emit", side_effect=OSError("fixture log failure")):
                result = task.run()
            self.assertEqual(result.stop_reason, "task_succeeded")

    def test_cli_progress_and_quiet_json_remain_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, _ = configured(root, mode="disabled")
            args = [sys.executable, str(ROOT / "main.py"), "run-task", str(path), "--task-id", "dev-001"]
            result = subprocess.run(args, encoding="utf-8", capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("[初始执行]", result.stderr)
            self.assertIn("evolution.txt", result.stdout)
            self.assertNotIn('"trajectory"', result.stdout + result.stderr)
            quiet = subprocess.run(args + ["--quiet", "--json"], encoding="utf-8", capture_output=True)
            self.assertEqual(quiet.returncode, 0, quiet.stderr)
            self.assertEqual(quiet.stderr, "")
            payload = json.loads(quiet.stdout)
            self.assertTrue(Path(payload["log_path"]).exists())
            self.assertNotEqual(json.loads(quiet.stdout)["run_root"], str(root / "runs/controller"))

    def test_saved_result_export_is_offline_and_does_not_duplicate_existing_log(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, startup = configured(root, rounds=1)
            task = startup.prepare_task("dev-001")
            attach_model(task, always_wrong=True)
            result = task.run()
            source = root / "result.json"
            source.write_text(json.dumps(startup.save_record(result)), encoding="utf-8")
            log_path = startup.execution.storage.run_root / "evolution.txt"
            log_path.unlink()
            event_file = startup.execution.storage.run_root / "experience/events.jsonl"
            original = event_file.read_bytes()
            with patch.object(task.execution.agent.model, "generate", side_effect=AssertionError("must be offline")):
                self.assertEqual(summarize_saved_run(source), log_path)
            content = log_path.read_bytes()
            self.assertIn("rejected", content.decode("utf-8"))
            self.assertIn("IMPROVED reusable instructions", content.decode("utf-8"))
            self.assertIn("旧版分 → 候选分", content.decode("utf-8"))
            self.assertEqual(event_file.read_bytes(), original)
            summarize_saved_run(source)
            self.assertEqual(log_path.read_bytes(), content)

    def test_multiple_candidates_distinguish_acceptance_selection_and_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            _, startup = configured(Path(directory), rounds=1, candidates=2)
            task = startup.prepare_task("dev-001")
            attach_model(task, candidates=2)
            result = task.run()
            text = (startup.execution.storage.run_root / "evolution.txt").read_text(encoding="utf-8")
            self.assertEqual(result.stop_reason, "task_succeeded")
            for alias in ("R1-C1", "R1-C2"):
                self.assertIn(f"【候选 {alias}】", text)
                self.assertIn(f"【{alias} 接受与选择】", text)
            self.assertEqual(text.count("实际提交：成功"), 1)
            self.assertIn("accepted；收益=", text)
            self.assertIn("committed；收益=", text)
            self.assertNotIn("报告异常", text)

    def test_incomplete_candidate_prevents_promotion_despite_another_passing_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            _, startup = configured(Path(directory), rounds=1, candidates=2)
            task = startup.prepare_task("dev-001")
            attach_model(task, candidates=2, mixed=True)
            result = task.run()
            text = (startup.execution.storage.run_root / "evolution.txt").read_text(encoding="utf-8")
            self.assertEqual(result.stop_reason, "validation_error")
            self.assertIn("通过接受规则：True", text)
            self.assertIn("实际提交数：0", text)
            self.assertNotIn("升级为稳定版本", text)

    def test_empty_generation_and_generation_error_still_finish_readable_report(self):
        for options in ({"empty": True}, {"generation_error": True}):
            with self.subTest(options=options), tempfile.TemporaryDirectory() as directory:
                _, startup = configured(Path(directory), rounds=1)
                task = startup.prepare_task("dev-001")
                attach_model(task, **options)
                result = task.run()
                text = (startup.execution.storage.run_root / "evolution.txt").read_text(encoding="utf-8")
                self.assertIn("无候选。", text)
                self.assertIn(result.stop_reason.value, text)
                self.assertIn("实际提交数：0", text)
                self.assertNotIn("报告异常", text)

    def test_tool_actions_and_step_limit_are_visible_without_full_model_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            _, startup = configured(Path(directory), mode="disabled")
            task = startup.prepare_task("dev-001")
            result = task.run()
            text = (startup.execution.storage.run_root / "evolution.txt").read_text(encoding="utf-8")
            self.assertIn("calculator", text)
            self.assertIn('"value":75', text)
            self.assertIn("step_limit", text)
            self.assertNotIn('"messages":', text)
            self.assertEqual(result.evolution_rounds_used, 0)

    def test_offline_export_preserves_all_episodes_in_execution_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, startup = configured(root, rounds=1, task_mode="continual")
            first = startup.prepare_task("dev-001")
            attach_model(first)
            first.run()
            second = startup.prepare_task("dev-002")
            attach_model(second, initial_success=True)
            result = second.run()
            source = root / "last.json"
            source.write_text(json.dumps(startup.save_record(result)), encoding="utf-8")
            report_path = startup.execution.storage.run_root / "evolution.txt"
            report_path.unlink()
            summarize_saved_run(source)
            text = report_path.read_text(encoding="utf-8")
            self.assertEqual(text.count("[任务记录结束："), 2)
            self.assertLess(text.index("任务：dev-001"), text.index("任务：dev-002"))
            self.assertIn("实际提交数：1", text)
            self.assertIn("提交后复测 1", text)

    def test_renderer_failure_cannot_change_evolution_outcome(self):
        with tempfile.TemporaryDirectory() as directory:
            _, startup = configured(Path(directory), rounds=1)
            task = startup.prepare_task("dev-001")
            attach_model(task)
            with patch.object(task.controller.progress, "record", side_effect=RuntimeError("fixture render failure")):
                result = task.run()
            self.assertEqual(result.stop_reason, "task_succeeded")
            self.assertNotEqual(result.initial_version_id, result.final_stable_version_id)


if __name__ == "__main__":
    unittest.main()
