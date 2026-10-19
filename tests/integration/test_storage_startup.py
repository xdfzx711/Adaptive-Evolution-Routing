import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

from aer_core.errors import StorageError
from experiments.storage import prepare_storage
from tests.storage.helpers import commit_request


ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "experiments/configs/v0_1_fixed_prompt.yaml"


class StorageStartupTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / "experiment.yaml"
        self.config = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
        self.config["storage"]["root"] = "runs"
        self.write_config()

    def write_config(self):
        self.path.write_text(yaml.safe_dump(self.config, allow_unicode=True), encoding="utf-8")

    def test_registered_storage_plugins_initialize_and_reopen_same_run(self):
        first = prepare_storage(self.path, run_id="example")
        reopened = prepare_storage(self.path, run_id="example")
        self.assertEqual(first.manifest, reopened.manifest)
        self.assertEqual(first.version_store.stable(), reopened.version_store.stable())
        self.assertEqual(len(first.manifest.plugins.instances), 3)
        self.assertTrue((first.run_root / "run_manifest.json").is_file())

    def test_new_runs_are_isolated_and_path_traversal_is_rejected(self):
        first, second = prepare_storage(self.path), prepare_storage(self.path)
        self.assertNotEqual(first.run_root, second.run_root)
        self.assertEqual(first.version_store.stable().version_id, second.version_store.stable().version_id)
        self.assertNotEqual(first.version_store.stable().artifacts["prompt"].uri, second.version_store.stable().artifacts["prompt"].uri)
        for run_id in ["../outside", "/absolute", "", "."]:
            with self.subTest(run_id=run_id):
                with self.assertRaises(StorageError):
                    prepare_storage(self.path, run_id=run_id)

    def test_changed_configuration_cannot_overwrite_existing_run(self):
        first = prepare_storage(self.path, run_id="example")
        original = self.config["agent"]["initial_prompt"].encode("utf-8")
        self.config["agent"]["initial_prompt"] = "different prompt"
        self.write_config()
        with self.assertRaises(StorageError):
            prepare_storage(self.path, run_id="example")
        self.assertEqual(first.artifact_store.get(first.version_store.stable().artifacts["prompt"]), original)

    def test_frozen_snapshot_imports_prompt_and_lineage_into_new_run(self):
        source = prepare_storage(self.path, run_id="source")
        request, _ = commit_request(source.artifact_store, source.version_store, source.version_store.stable())
        request = request.replace(run_id=source.run_id)
        source.version_store.commit(request)
        snapshot_ref = source.version_store.save(request.validated_snapshot)
        self.config["evolution"]["mode"] = "disabled"
        self.config["limits"]["max_evolution_rounds"] = 0
        self.config["routing"] = {"plugin": None, "fixed_target": None, "manual_decisions": []}
        self.config["targets"] = {"allowed": [], "default_operators": {}}
        self.config["benchmark"]["execution_split"] = "test"
        self.config["agent"]["initial_prompt"] = None
        self.config["agent"]["initial_snapshot_ref"] = str(source.run_root / snapshot_ref.uri)
        self.write_config()
        imported = prepare_storage(self.path, run_id="test-run")
        self.assertEqual(imported.version_store.stable(), request.validated_snapshot)
        self.assertEqual(imported.version_store.load(source.initial_snapshot_ref.record_id), source.version_store.load(source.initial_snapshot_ref.record_id))
        self.assertEqual(imported.artifact_store.get(request.validated_snapshot.artifacts["prompt"]), source.artifact_store.get(request.validated_snapshot.artifacts["prompt"]))

    def test_init_storage_cli_is_utf8_and_does_not_execute_agents(self):
        command = [sys.executable, str(ROOT / "main.py"), "init-storage", str(self.path), "--run-id", "cli-run"]
        result = subprocess.run(command, cwd=self.root, capture_output=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "storage_initialized")
        self.assertEqual(Path(payload["run_root"]), self.root / "runs/cli-run")
        self.assertTrue((self.root / "runs/cli-run/run_manifest.json").is_file())
        self.assertFalse((self.root / "runs/cli-run/experience/events.jsonl").exists())
