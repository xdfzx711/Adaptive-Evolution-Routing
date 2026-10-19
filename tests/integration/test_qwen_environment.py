import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
import yaml
from pydantic import SecretStr

from aer_core.errors import PluginConfigurationError
from experiments.bootstrap import agent_bindings, assemble_experiment, create_registry
from experiments.configuration import load_experiment_config
from experiments.environment import env_file_path, provider_dependencies
from experiments.execution import prepare_execution
from plugins.providers.qwen import QwenCredentials
from tests.execution.test_qwen import FAKE_KEY, completion


ROOT = Path(__file__).resolve().parents[2]


class QwenEnvironmentTests(unittest.TestCase):
    def config(self, root, env_path="credentials.env", single=False):
        template = "v0_1_qwen_single_call.yaml" if single else "v0_1_qwen_react.yaml"
        data = yaml.safe_load((ROOT / "experiments/configs" / template).read_text(encoding="utf-8"))
        data["providers"]["model"]["config"]["dotenv_path"] = env_path
        data["storage"]["root"] = str(root / "runs")
        path = root / "experiment.yaml"
        path.write_text(yaml.safe_dump(data), encoding="utf-8")
        return path

    def test_env_path_is_relative_to_yaml_not_current_directory(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            root = Path(directory)
            config = self.config(root)
            env = root / "credentials.env"
            env.write_text(f'QWEN_API_KEY="{FAKE_KEY}"\nQWEN_BASE_URL=https://qwen.example/v1\nQWEN_MODEL=qwen-test-model\n', encoding="utf-8")
            loaded = load_experiment_config(config)
            self.assertEqual(env_file_path(loaded), env)
            deps = provider_dependencies(loaded, agent_bindings(loaded.config, create_registry()))
            self.assertEqual(deps["model_credentials"].api_key.get_secret_value(), FAKE_KEY)
            self.assertNotIn("QWEN_API_KEY", os.environ)
            self.assertNotIn(FAKE_KEY, loaded.config.model_dump_json())
            self.assertEqual(loaded.config.providers.model.model_id, "qwen-test-model")

    def test_process_environment_overrides_file_and_empty_values_fail(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            root = Path(directory)
            config = self.config(root)
            (root / "credentials.env").write_text(f"QWEN_API_KEY={FAKE_KEY}\nQWEN_BASE_URL=https://file.example/v1\nQWEN_MODEL=qwen-plus\n", encoding="utf-8")
            loaded = load_experiment_config(config)
            bindings = agent_bindings(loaded.config, create_registry())
            os.environ["QWEN_BASE_URL"] = "https://process.example/v1"
            deps = provider_dependencies(loaded, bindings)
            self.assertEqual(deps["model_credentials"].base_url, "https://process.example/v1")
            os.environ["QWEN_API_KEY"] = ""
            with self.assertRaisesRegex(PluginConfigurationError, "QWEN_API_KEY"):
                provider_dependencies(loaded, bindings)

    def test_missing_env_is_clear_before_creating_run_directory(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            root = Path(directory)
            config = self.config(root)
            load_experiment_config(config)  # Validation needs no credentials.
            with self.assertRaises(PluginConfigurationError) as caught:
                prepare_execution(config)
            self.assertIn("QWEN_MODEL", str(caught.exception))
            self.assertFalse((root / "runs").exists())

    def test_dotenv_values_are_not_interpolated_or_persisted(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            root = Path(directory)
            config = self.config(root)
            value = "key-with-${LITERAL}-suffix"
            (root / "credentials.env").write_text(f'QWEN_API_KEY="{value}"\nQWEN_BASE_URL=https://qwen.example/v1\nQWEN_MODEL=qwen-plus\n', encoding="utf-8")
            loaded = load_experiment_config(config)
            deps = provider_dependencies(loaded, agent_bindings(loaded.config, create_registry()))
            self.assertEqual(deps["model_credentials"].api_key.get_secret_value(), value)
            prepared = prepare_execution(config, run_id="qwen")
            manifest = (prepared.storage.run_root / "run_manifest.json").read_text(encoding="utf-8")
            self.assertNotIn(value, manifest)
            self.assertEqual(json.loads(manifest)["execution"]["model_runtime"]["endpoint"], "https://qwen.example/v1/chat/completions")

    def test_explicit_credentials_can_be_injected_without_dotenv(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            root = Path(directory)
            config = self.config(root)
            data = yaml.safe_load(config.read_text(encoding="utf-8"))
            data["providers"]["model"]["model_id"] = "qwen-plus"
            config.write_text(yaml.safe_dump(data), encoding="utf-8")
            assembled = assemble_experiment(config, dependencies={
                "model_credentials": QwenCredentials(SecretStr(FAKE_KEY), "https://qwen.example/v1")})
            self.assertEqual(assembled.plugins.agent.snapshot().agent_plugin_id, "react_agent")
            self.assertNotIn(FAKE_KEY, assembled.built_plugins.manifest.model_dump_json())

    def test_both_agents_use_qwen_through_config_and_report_actual_usage(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            root = Path(directory)
            (root / "credentials.env").write_text(f"QWEN_API_KEY={FAKE_KEY}\nQWEN_BASE_URL=https://qwen.example/v1\nQWEN_MODEL=qwen-plus\n", encoding="utf-8")
            for single in (False, True):
                prepared = prepare_execution(self.config(root, single=single), run_id="single" if single else "react")
                # Keep the HTTP boundary local; no remote inference is performed.
                prepared.built_plugins.instances["model"]._transport = httpx.MockTransport(lambda request: httpx.Response(200, json=completion()))
                record = prepared.run_task("dev-001")
                self.assertTrue(record.evaluation.success)
                self.assertEqual(record.execution.usage.total_tokens, 125)
                self.assertEqual(record.execution.usage.usage_sources, ("provider_reported",))
                self.assertNotIn(FAKE_KEY, json.dumps(prepared.save_record(record)))

    def test_cli_output_cannot_overwrite_dotenv(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root)
            env = root / "credentials.env"
            contents = f"QWEN_API_KEY={FAKE_KEY}\nQWEN_BASE_URL=https://qwen.example/v1\n"
            env.write_text(contents, encoding="utf-8")
            result = subprocess.run([sys.executable, str(ROOT / "main.py"), "inspect-benchmark", str(config),
                                     "--output", str(env)], cwd=root, capture_output=True, encoding="utf-8")
            self.assertEqual(result.returncode, 2)
            self.assertNotIn(FAKE_KEY, result.stderr)
            self.assertEqual(env.read_text(encoding="utf-8"), contents)

    def test_cli_missing_credentials_reports_setup_error_without_traceback(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            root = Path(directory)
            config = self.config(root)
            result = subprocess.run([sys.executable, str(ROOT / "main.py"), "execute-task", str(config),
                                     "--task-id", "dev-001"], cwd=root, capture_output=True, encoding="utf-8")
            self.assertEqual(result.returncode, 2)
            self.assertIn("QWEN_MODEL", result.stderr)
            self.assertNotIn("Traceback", result.stderr)
            self.assertFalse((root / "runs").exists())

    def test_env_model_is_used_in_request_snapshot_and_manifest(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            root = Path(directory)
            env = root / "credentials.env"
            env.write_text(f"QWEN_API_KEY={FAKE_KEY}\nQWEN_BASE_URL=https://qwen.example/v1\nQWEN_MODEL=qwen-env-model\n", encoding="utf-8")
            prepared = prepare_execution(self.config(root), run_id="from-env")
            model = prepared.built_plugins.instances["model"]
            def handler(request):
                self.assertEqual(json.loads(request.content)["model"], "qwen-env-model")
                return httpx.Response(200, json=completion())
            model._transport = httpx.MockTransport(handler)
            self.assertTrue(prepared.run_task("dev-001").evaluation.success)
            self.assertEqual(prepared.storage.version_store.stable().config["model"]["model_id"], "qwen-env-model")
            manifest = json.loads((prepared.storage.run_root / "run_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["execution"]["model_runtime"]["model_id"], "qwen-env-model")

    def test_process_model_override_and_empty_model_are_respected(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            root = Path(directory)
            config = self.config(root)
            (root / "credentials.env").write_text("QWEN_MODEL=qwen-file-model\n", encoding="utf-8")
            os.environ["QWEN_MODEL"] = "qwen-process-model"
            self.assertEqual(load_experiment_config(config).config.providers.model.model_id, "qwen-process-model")
            os.environ["QWEN_MODEL"] = ""
            with self.assertRaisesRegex(PluginConfigurationError, "QWEN_MODEL"):
                prepare_execution(config)

    def test_missing_model_with_present_credentials_does_not_use_yaml_placeholder(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            root = Path(directory)
            config = self.config(root)
            (root / "credentials.env").write_text(f"QWEN_API_KEY={FAKE_KEY}\nQWEN_BASE_URL=https://qwen.example/v1\n", encoding="utf-8")
            with self.assertRaisesRegex(PluginConfigurationError, "QWEN_MODEL"):
                prepare_execution(config)
            self.assertFalse((root / "runs").exists())
