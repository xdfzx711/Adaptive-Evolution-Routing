import json
from pathlib import Path
import sys
import tempfile
import unittest

from experiments.baselines.oscopilot import prepare_run, run_native
from plugins.evolution.oscopilot.source import DEFAULT_SOURCE, UPSTREAM_REVISION


@unittest.skipUnless((DEFAULT_SOURCE / "oscopilot").is_dir(), "initialize the OS-Copilot submodule")
class NativeBaselineTests(unittest.TestCase):
    def test_prepare_keeps_native_source_and_repository_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = root / "task"
            task.mkdir()
            run, manifest = prepare_run(python=sys.executable, query="Inspect the public task input", workspace=task,
                                        output_root=root / "baseline")
            self.assertEqual(manifest["upstream_revision"], UPSTREAM_REVISION)
            self.assertEqual(Path(manifest["command"][1]), DEFAULT_SOURCE / "quick_start.py")
            self.assertEqual(manifest["source_patches"], [])
            self.assertEqual(json.loads((run / "tool-repository/generated_tools.json").read_text()), {})
            self.assertIsNone(manifest["usage"])
            with self.assertRaises(FileExistsError):
                prepare_run(python=sys.executable, query="same", workspace=task, output_root=run)

    def test_process_completion_is_not_reported_as_task_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = root / "task"
            task.mkdir()
            run, manifest = prepare_run(python=sys.executable, query="Task", workspace=task, output_root=root / "baseline")
            # A process fixture validates launch/receipt semantics, without claiming a native model run.
            manifest["command"] = [sys.executable, "-c", "from pathlib import Path; print('fixture process completed'); print(Path.cwd())"]
            result = run_native(run, manifest, timeout_seconds=10)
            self.assertEqual(result["status"], "process_completed")
            self.assertIsNone(result["task_success"])
            self.assertIsNone(result["usage"])
            self.assertIn("fixture process completed", (run / "stdout.txt").read_text())
            self.assertIn(str(task), (run / "stdout.txt").read_text())
