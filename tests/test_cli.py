import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "experiments/configs/v0_1_fixed_prompt.yaml"
INDEX = ROOT / "tests/fixtures/benchmark_index.json"


class CliTests(unittest.TestCase):
    def run_cli(self, *args, cwd=ROOT):
        return subprocess.run([sys.executable, str(ROOT / "main.py"), *map(str, args)], cwd=cwd, capture_output=True, encoding="utf-8")

    def test_prints_resolved_json(self):
        result = self.run_cli("validate-config", CONFIG)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["config"]["storage"]["root"], str(ROOT / "runs"))
        self.assertEqual(payload["config"]["evolution"]["mode"], "fixed")

    def test_index_check_and_output_file_from_another_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "resolved.json"
            result = self.run_cli("validate-config", CONFIG, "--benchmark-index", INDEX, "--output", output, cwd=directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["checks"]["task_partitions"], "index_checked")
            self.assertEqual(payload["config"]["storage"]["root"], str(ROOT / "runs"))

    def test_error_has_field_path_exit_code_and_no_traceback(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "invalid.yaml"
            text = CONFIG.read_text(encoding="utf-8").replace("max_evolution_rounds: 3", "max_evolution_rounds: -1")
            config.write_text(text, encoding="utf-8")
            result = self.run_cli("validate-config", config)
            self.assertEqual(result.returncode, 2)
            self.assertIn("limits.max_evolution_rounds", result.stderr)
            self.assertNotIn("Traceback", result.stderr)
            self.assertEqual(result.stdout, "")

    def test_output_error_is_clear(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_cli("validate-config", CONFIG, "--output", directory)
            self.assertEqual(result.returncode, 2)
            self.assertIn("Cannot write", result.stderr)
            self.assertNotIn("Traceback", result.stderr)

    def test_output_cannot_overwrite_input_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "experiment.yaml"
            original = CONFIG.read_text(encoding="utf-8")
            config.write_text(original, encoding="utf-8")
            result = self.run_cli("validate-config", config, "--output", config)
            self.assertEqual(result.returncode, 2)
            self.assertIn("must not overwrite", result.stderr)
            self.assertEqual(config.read_text(encoding="utf-8"), original)

    def test_help_version_and_no_experiment_execution_command(self):
        help_result = self.run_cli("--help")
        self.assertEqual(help_result.returncode, 0)
        self.assertIn("validate-config", help_result.stdout)
        version_result = self.run_cli("--version")
        self.assertEqual(version_result.returncode, 0)
        self.assertIn("0.1.0", version_result.stdout)
        self.assertEqual(self.run_cli("run", CONFIG).returncode, 2)
