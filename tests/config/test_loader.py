import copy
import json
import tempfile
import unittest
from pathlib import Path

import yaml

from aer_core.config.catalog import ConfigCatalog, PluginDeclaration
from aer_core.errors import ConfigError
from experiments.configuration import load_experiment_config


ROOT = Path(__file__).resolve().parents[2]
CONFIGS = ROOT / "experiments" / "configs"
INDEX = ROOT / "tests" / "fixtures" / "benchmark_index.json"


class ConfigLoaderTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "experiment.yaml"
        self.index_path = Path(self.directory.name) / "index.json"
        self.config = yaml.safe_load((CONFIGS / "v0_1_fixed_prompt.yaml").read_text(encoding="utf-8"))
        self.index = json.loads(INDEX.read_text(encoding="utf-8"))

    def load(self, config=None, index=None):
        self.path.write_text(yaml.safe_dump(config if config is not None else self.config, allow_unicode=True), encoding="utf-8")
        if index is None:
            return load_experiment_config(self.path)
        self.index_path.write_text(json.dumps(index), encoding="utf-8")
        return load_experiment_config(self.path, benchmark_index=self.index_path)

    def assert_issue(self, field, config=None, index=None):
        with self.assertRaises(ConfigError) as caught:
            self.load(config, index)
        self.assertTrue(any(issue.field == field for issue in caught.exception.issues), str(caught.exception))

    def test_all_example_modes(self):
        modes = set()
        for path in CONFIGS.glob("v0_1_*.yaml"):
            # Native SkillClaw has a separate runner schema and its own loader tests.
            if path.name == "v0_1_codex_skillclaw_baseline.yaml":
                continue
            with self.subTest(path=path.name):
                loaded = load_experiment_config(path)
                modes.add(loaded.config.evolution.mode)
                self.assertEqual(Path(loaded.config.storage.root), ROOT / "runs")
                self.assertEqual(loaded.as_dict()["checks"]["task_partitions"], "split_names_and_mode_checked")
        self.assertEqual(modes, {"disabled", "fixed", "manual", "llm"})

    def test_round_limits_are_configurable(self):
        for rounds in [0, 1, 7, 1000]:
            with self.subTest(rounds=rounds):
                self.config["limits"]["max_evolution_rounds"] = rounds
                self.assertEqual(self.load().config.limits.max_evolution_rounds, rounds)

    def test_history_limits_are_configurable_and_validated(self):
        self.config["evolution"].update(history_max_attempts=0, history_max_text_chars=640, history_max_change_chars=8000)
        loaded = self.load().config.evolution
        self.assertEqual((loaded.history_max_attempts, loaded.history_max_text_chars), (0, 640))
        self.assertEqual(loaded.history_max_change_chars, 8000)
        for field, value in (("history_max_attempts", -1), ("history_max_text_chars", 0), ("history_max_change_chars", 0)):
            invalid = copy.deepcopy(self.config)
            invalid["evolution"][field] = value
            self.assert_issue(f"evolution.{field}", invalid)

    def test_success_stop_is_enabled_by_default_and_configurable(self):
        self.assertTrue(self.load().config.evolution.stop_on_success)
        self.config["evolution"]["stop_on_success"] = False
        self.assertFalse(self.load().config.evolution.stop_on_success)
        self.config["evolution"]["stop_on_success"] = "false"
        self.assert_issue("evolution.stop_on_success")

    def test_manual_zero_rounds_and_short_plan(self):
        manual = yaml.safe_load((CONFIGS / "v0_1_manual_prompt.yaml").read_text(encoding="utf-8"))
        manual["routing"]["manual_decisions"] = manual["routing"]["manual_decisions"][:1]
        self.assertEqual(len(self.load(manual).config.routing.manual_decisions), 1)
        manual["limits"]["max_evolution_rounds"] = 0
        manual["routing"]["manual_decisions"] = []
        self.assertEqual(self.load(manual).config.limits.max_evolution_rounds, 0)

    def test_required_fields_have_no_implicit_defaults(self):
        missing = copy.deepcopy(self.config)
        del missing["limits"]["max_evolution_rounds"]
        self.assert_issue("limits.max_evolution_rounds", missing)
        missing = copy.deepcopy(self.config)
        del missing["agent"]["initial_snapshot_ref"]
        self.assert_issue("agent.initial_snapshot_ref", missing)

    def test_unknown_fields_and_strict_integer_types(self):
        invalid = copy.deepcopy(self.config)
        invalid["limits"]["max_rounds"] = 3
        self.assert_issue("limits.max_rounds", invalid)
        for value in [True, "3", 3.0, -1]:
            with self.subTest(value=value):
                invalid = copy.deepcopy(self.config)
                invalid["limits"]["max_evolution_rounds"] = value
                self.assert_issue("limits.max_evolution_rounds", invalid)

    def test_budget_bounds_and_output_cap(self):
        for key, value in [("max_tokens", 0), ("max_tool_calls", -1), ("max_elapsed_seconds", 0), ("model_call_timeout_seconds", float("inf")), ("max_consecutive_no_commit_rounds", 0)]:
            with self.subTest(key=key):
                invalid = copy.deepcopy(self.config)
                invalid["limits"][key] = value
                self.assert_issue(f"limits.{key}", invalid)
        self.config["limits"]["max_output_tokens_per_call"] = self.config["limits"]["max_tokens"] + 1
        self.assert_issue("limits.max_output_tokens_per_call")

    def test_acceptance_thresholds_are_finite_and_bounded(self):
        for key in ["min_improvement", "max_regression_drop"]:
            for value in [-0.01, 1.01, float("nan"), float("inf"), True]:
                with self.subTest(key=key, value=value):
                    invalid = copy.deepcopy(self.config)
                    invalid["validation"][key] = value
                    self.assert_issue(f"validation.{key}", invalid)
        self.config["validation"]["min_improvement"] = 0
        self.config["validation"]["max_regression_drop"] = 1
        self.load()

    def test_unknown_plugin_reference_and_provider_config(self):
        self.config["agent"]["plugin"] = "missing_agent"
        self.assert_issue("agent.plugin")
        self.config["agent"]["plugin"] = "react_agent"
        self.config["providers"]["model"]["config"]["unexpected"] = 1
        self.assert_issue("providers.model.config.unexpected")

    def test_invalid_operator_tool_and_policy_version(self):
        invalid = copy.deepcopy(self.config)
        invalid["targets"]["default_operators"]["prompt"] = "missing_evolver"
        self.assert_issue("targets.default_operators.prompt", invalid)
        invalid = copy.deepcopy(self.config)
        invalid["providers"]["tools"]["enabled"] = ["unknown_tool"]
        self.assert_issue("providers.tools.enabled[0]", invalid)
        invalid = copy.deepcopy(self.config)
        invalid["validation"]["policy_version"] = "v99"
        self.assert_issue("validation.policy_version", invalid)

    def test_mode_router_and_target_compatibility(self):
        self.config["routing"]["plugin"] = "manual_router"
        self.assert_issue("routing.plugin")
        self.config["routing"]["plugin"] = "fixed_router"
        self.config["targets"]["allowed"] = ["memory"]
        self.config["targets"]["default_operators"] = {"memory": "simple_prompt_evolver"}
        self.config["routing"]["fixed_target"] = "memory"
        self.assert_issue("targets.default_operators.memory")

    def test_disabled_mode_rejects_evolution_state(self):
        disabled = yaml.safe_load((CONFIGS / "v0_1_no_evolution.yaml").read_text(encoding="utf-8"))
        disabled["limits"]["max_evolution_rounds"] = 1
        self.assert_issue("limits.max_evolution_rounds", disabled)
        disabled["limits"]["max_evolution_rounds"] = 0
        disabled["routing"]["plugin"] = "fixed_router"
        self.assert_issue("routing.plugin", disabled)

    def test_memory_evolution_requires_configured_bank_and_embeddings(self):
        self.config["targets"]["allowed"] = ["memory"]
        self.config["targets"]["default_operators"] = {"memory": "reasoningbank_memory_evolver"}
        self.config["routing"]["fixed_target"] = "memory"
        self.assert_issue("agent.config.memory")
        self.config["agent"]["config"] = {"memory": {"embedding": {"model_id": "embedding-fixture"}}}
        self.assertEqual(self.load().config.agent.config["memory"]["embedding"]["model_id"], "embedding-fixture")

    def test_manual_plan_order_length_and_targets(self):
        manual = yaml.safe_load((CONFIGS / "v0_1_manual_prompt.yaml").read_text(encoding="utf-8"))
        invalid = copy.deepcopy(manual)
        invalid["routing"]["manual_decisions"][1]["round_id"] = 1
        self.assert_issue("routing.manual_decisions", invalid)
        invalid = copy.deepcopy(manual)
        invalid["limits"]["max_evolution_rounds"] = 1
        self.assert_issue("routing.manual_decisions", invalid)
        invalid = copy.deepcopy(manual)
        invalid["routing"]["manual_decisions"][0]["target"] = "memory"
        self.assert_issue("routing.manual_decisions[0].target", invalid)

    def test_duplicate_lists_and_empty_task_selection(self):
        self.config["benchmark"]["task_ids"] = ["dev-001", "dev-001"]
        self.assert_issue("benchmark.task_ids")
        self.config["benchmark"]["task_ids"] = []
        self.assert_issue("benchmark.task_ids")

    def test_prompt_and_snapshot_are_exclusive(self):
        self.config["agent"]["initial_prompt"] = None
        self.assert_issue("agent.initial_prompt")
        self.config["agent"]["initial_prompt"] = " "
        self.assert_issue("agent.initial_prompt")

    def test_single_call_agent_requires_one_step(self):
        self.config["agent"]["plugin"] = "single_call_agent"
        self.assert_issue("agent.max_steps")
        self.config["agent"]["max_steps"] = 1
        self.load()

    def test_split_names_group_names_and_final_test_mode(self):
        invalid = copy.deepcopy(self.config)
        invalid["validation"]["split"] = "dev"
        self.assert_issue("validation.split", invalid)
        invalid = copy.deepcopy(self.config)
        invalid["validation"]["regression_group"] = "similar"
        self.assert_issue("validation.regression_group", invalid)
        invalid = copy.deepcopy(self.config)
        invalid["benchmark"]["execution_split"] = "test"
        self.assert_issue("benchmark.execution_split", invalid)

    def test_frozen_snapshot_path_resolution_and_missing_file(self):
        disabled = yaml.safe_load((CONFIGS / "v0_1_no_evolution.yaml").read_text(encoding="utf-8"))
        disabled["benchmark"]["execution_split"] = "test"
        disabled["agent"]["initial_prompt"] = None
        disabled["agent"]["initial_snapshot_ref"] = "snapshot.json"
        self.assert_issue("agent.initial_snapshot_ref", disabled)
        snapshot = self.path.parent / "snapshot.json"
        snapshot.write_text("{}", encoding="utf-8")
        loaded = self.load(disabled, self.index)
        self.assertEqual(loaded.config.agent.initial_snapshot_ref, str(snapshot))
        self.assertTrue(any("Snapshot contents" in note for note in loaded.as_dict()["notes"]))

    def test_valid_index_and_selected_task(self):
        self.config["benchmark"]["task_ids"] = ["dev-001"]
        loaded = self.load(index=self.index)
        self.assertEqual(loaded.as_dict()["checks"]["task_partitions"], "index_checked")

    def test_index_detects_cross_split_identity_and_source_overlap(self):
        self.index["splits"]["test"][0]["task_id"] = "dev-001"
        self.assert_issue("benchmark_index.splits.test[0].task_id", index=self.index)
        self.index["splits"]["test"][0]["task_id"] = "test-001"
        self.index["splits"]["test"][0]["source_id"] = "source-dev-001"
        self.assert_issue("benchmark_index.splits.test[0].source_id", index=self.index)

    def test_index_checks_task_membership_and_benchmark_identity(self):
        self.config["benchmark"]["task_ids"] = ["test-001"]
        self.assert_issue("benchmark.task_ids[0]", index=self.index)
        self.config["benchmark"]["task_ids"] = None
        self.index["benchmark_id"] = "another_benchmark"
        self.assert_issue("benchmark_index.benchmark_id", index=self.index)

    def test_index_requires_nonempty_correct_validation_groups(self):
        self.index["splits"]["validation"][1]["metadata"]["validation_group"] = "similar"
        self.assert_issue("validation", index=self.index)
        self.index["splits"]["validation"][1]["metadata"] = {}
        self.assert_issue("benchmark_index.splits.validation[1].metadata.validation_group", index=self.index)

    def test_disabled_does_not_require_validation_tasks(self):
        disabled = yaml.safe_load((CONFIGS / "v0_1_no_evolution.yaml").read_text(encoding="utf-8"))
        self.index["splits"]["validation"] = []
        self.load(disabled, self.index)

    def test_missing_split_and_empty_execution_split(self):
        del self.index["splits"]["test"]
        self.assert_issue("benchmark_index.splits", index=self.index)
        self.index = json.loads(INDEX.read_text(encoding="utf-8"))
        self.index["splits"]["dev"] = []
        self.assert_issue("benchmark.execution_split", index=self.index)

    def test_invalid_yaml_duplicate_keys_and_non_mapping_root(self):
        for text, message in [("experiment: [", "$file"), ("schema_version: aer.v0.1\nschema_version: aer.v0.1\n", "duplicate YAML key"), ("experiment:\n  seed: 1\n  seed: 2\n", "duplicate YAML key"), ("- item\n", "must be a non-empty mapping"), ("!!python/object/apply:builtins.print ['no']", "$file")]:
            with self.subTest(text=text):
                self.path.write_text(text, encoding="utf-8")
                with self.assertRaises(ConfigError) as caught:
                    load_experiment_config(self.path)
                self.assertIn(message, str(caught.exception))

    def test_missing_file_and_invalid_index_json(self):
        with self.assertRaises(ConfigError):
            load_experiment_config(self.path)
        self.load()
        self.index_path.write_text('{"benchmark_id":"toy_tasks","benchmark_id":"other"}', encoding="utf-8")
        with self.assertRaises(ConfigError) as caught:
            load_experiment_config(self.path, benchmark_index=self.index_path)
        self.assertIn("duplicate JSON key", str(caught.exception))

    def test_catalog_rejects_duplicate_declarations(self):
        entry = PluginDeclaration("agent", "react_agent")
        with self.assertRaisesRegex(ValueError, "duplicate"):
            ConfigCatalog((entry, entry))
