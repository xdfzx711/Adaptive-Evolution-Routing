from pathlib import Path
import tempfile
import unittest

import yaml

from experiments.bootstrap import assemble_experiment
from experiments.configuration import load_experiment_config
from experiments.execution import prepare_execution
from experiments.storage import prepare_storage
from plugins.skills.library import decode_library


ROOT = Path(__file__).resolve().parents[2]


class SkillDefaultTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "experiment.yaml"
        self.data = yaml.safe_load((ROOT / "experiments/configs/v0_1_no_evolution.yaml").read_text(encoding="utf-8"))
        self.data["storage"]["root"] = "runs"

    def write(self):
        self.path.write_text(yaml.safe_dump(self.data), encoding="utf-8")
        return self.path

    def test_all_supported_agents_freeze_default_library_from_external_config_directory(self):
        for plugin in ("react_agent", "single_call_agent", "codex_cli_agent"):
            with self.subTest(plugin=plugin):
                self.data["agent"]["plugin"] = plugin
                self.data["agent"]["max_steps"] = 1 if plugin == "single_call_agent" else 8
                if plugin == "codex_cli_agent":
                    self.data["agent"].pop("max_steps")
                prepared = prepare_storage(self.write(), run_id=plugin)
                snapshot = prepared.version_store.stable()
                library = decode_library(prepared.artifact_store.get(snapshot.artifacts["skill"]))
                self.assertEqual([skill.name for skill in library.skills], ["task-output-contract"])
                skills = prepared.loaded_config.config.agent.config["skills"]
                self.assertEqual(Path(skills["initial_library"]), ROOT / "experiments/skills")
                self.assertEqual(snapshot.config["skills"]["mode"],
                                 "preload" if plugin == "single_call_agent" else "on_demand")
                self.assertEqual(load_experiment_config(prepared.loaded_config), prepared.loaded_config)

    def test_explicit_disable_empty_and_custom_library_override_defaults(self):
        custom = self.root / "custom-skills"
        custom.mkdir()
        for index, (settings, expected) in enumerate(((None, None), ({}, None),
                                                     ({"initial_library": "custom-skills"}, str(custom)))):
            with self.subTest(settings=settings):
                self.data["agent"]["config"] = {"skills": settings}
                prepared = prepare_storage(self.write(), run_id=f"override-{index}")
                snapshot = prepared.version_store.stable()
                if settings is None:
                    self.assertNotIn("skill", snapshot.artifacts)
                else:
                    self.assertFalse(decode_library(prepared.artifact_store.get(snapshot.artifacts["skill"])).skills)
                    self.assertEqual(prepared.loaded_config.config.agent.config["skills"].get("initial_library"), expected)
        loaded = load_experiment_config(self.path, overrides=["agent.config.skills=null"])
        self.assertIsNone(loaded.config.agent.config["skills"])

    def test_default_execution_and_assembly_agree_and_single_call_preloads(self):
        for plugin in ("react_agent", "single_call_agent"):
            with self.subTest(plugin=plugin):
                self.data["agent"]["plugin"] = plugin
                self.data["agent"]["max_steps"] = 1 if plugin == "single_call_agent" else 8
                prepared = prepare_execution(self.write(), run_id=plugin)
                result = prepared.run_task("dev-001")
                self.assertTrue(result.evaluation.success)
                self.assertEqual(result.execution.state_summary["available_skills"], ("task-output-contract",))
                self.assertEqual(result.execution.state_summary["injected_skills"],
                                 ("task-output-contract",) if plugin == "single_call_agent" else ())
                assembled = assemble_experiment(self.path)
                self.assertEqual(assembled.plugins.agent.snapshot().config, prepared.storage.version_store.stable().config)

    def test_default_snapshot_restore_preserves_frozen_library(self):
        original = prepare_storage(self.write(), run_id="original")
        snapshot = original.version_store.stable()
        self.data["agent"]["initial_prompt"] = None
        self.data["agent"]["initial_snapshot_ref"] = str(original.run_root / original.initial_snapshot_ref.uri)
        restored = prepare_storage(self.write(), run_id="restored")
        self.assertEqual(restored.version_store.stable(), snapshot)
        self.assertEqual(restored.artifact_store.get(snapshot.artifacts["skill"]),
                         original.artifact_store.get(snapshot.artifacts["skill"]))
