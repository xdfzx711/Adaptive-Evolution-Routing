"""Jev plugs into the existing controller without changing its control flow."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from aer_core.errors import ConfigError, PluginConfigurationError
from experiments.configuration import load_experiment_config
from experiments.orchestration import prepare_orchestrator
from plugins.routers.jev import JevRouter
from plugins.storage.layout import episode_directory
from tests.orchestration.helpers import ROOT, attach_model


class JevPluginIntegrationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        config = yaml.safe_load((ROOT / "experiments/configs/v0_1_fixed_prompt.yaml").read_text(encoding="utf-8"))
        config["storage"]["root"] = str(self.root / "runs")
        config["limits"]["max_evolution_rounds"] = 1
        config["agent"]["max_steps"] = 1
        config["evolution"]["mode"] = "llm"
        config["routing"] = {"plugin": "jev_router", "fixed_target": None,
            "manual_decisions": [], "config": {"dotenv_path": ".env"}}
        self.config_path = self.root / "config.yaml"
        self.config_path.write_text(yaml.safe_dump(config), encoding="utf-8")

    def test_one_target_routes_and_reports_without_an_api_call(self):
        (self.root / ".env").write_text(
            "JEV_API_KEY=test-key\nJEV_BASE_URL=https://jev.example\nJEV_MODEL=jev-test\n", encoding="utf-8")
        with patch.dict("os.environ", {"JEV_API_KEY": "test-key", "JEV_BASE_URL": "https://jev.example",
            "JEV_MODEL": "jev-test"}):
            startup = prepare_orchestrator(self.config_path, run_id="jev-controller")
            task = startup.prepare_task("dev-001")
        self.assertIsInstance(task.evolution.router, JevRouter)
        attach_model(task)
        result = task.run()
        self.assertEqual(result.stop_reason, "task_succeeded")
        self.assertEqual(result.evolution_rounds_used, 1)
        report_path = (task.execution.storage.run_root / episode_directory("dev-001", result.episode_id)
            / "round-001" / "record.json")
        report = json.loads(report_path.read_text(encoding="utf-8"))
        receipt = report["routing"]["decision_call"]
        self.assertEqual(receipt["source"], "single_feasible_target")
        self.assertEqual(receipt["usage"]["model_calls"], 0)
        self.assertEqual(report["routing"]["decision"]["target"], "prompt")
        self.assertNotIn("test-key", report_path.read_text(encoding="utf-8"))

    def test_missing_jev_environment_is_reported_during_plugin_assembly(self):
        with patch.dict("os.environ", {}, clear=True), self.assertRaises(PluginConfigurationError) as caught:
            startup = prepare_orchestrator(self.config_path, run_id="jev-missing-env")
            startup.prepare_task("dev-001")
        self.assertIn("JEV_API_KEY", str(caught.exception))

    def test_plugin_specific_configuration_is_validated_before_assembly(self):
        config = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        config["routing"]["config"]["max_state_chars"] = 0
        self.config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
        with self.assertRaises(ConfigError) as caught:
            load_experiment_config(self.config_path)
        self.assertTrue(any(issue.field == "routing.config.max_state_chars"
            for issue in caught.exception.issues))


if __name__ == "__main__":
    unittest.main()
