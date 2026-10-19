"""CLI assembly integration tests."""

import json
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class AssemblyCliTests(unittest.TestCase):
    def cli(self, filename):
        return subprocess.run([sys.executable, str(ROOT / "main.py"), "assemble-config", str(ROOT / "experiments/configs" / filename)], capture_output=True, encoding="utf-8")

    def test_three_mock_templates_assemble_from_cli(self):
        for filename in ["mock_fixed_a.yaml", "mock_fixed_b.yaml", "mock_manual.yaml"]:
            with self.subTest(filename=filename):
                result = self.cli(filename)
                self.assertEqual(result.returncode, 0, result.stderr)
                payload = json.loads(result.stdout)
                self.assertEqual(payload["status"], "assembled")
                self.assertEqual(len(payload["assembly_manifest"]["instances"]), 3)

    def test_fixed_and_manual_real_plugins_assemble(self):
        for name in ("v0_1_fixed_prompt.yaml", "v0_1_manual_prompt.yaml"):
            result = self.cli(name)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(json.loads(result.stdout)["assembly_manifest"]["instances"]), 5)
