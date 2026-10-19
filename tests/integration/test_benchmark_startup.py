import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

from aer_core.errors import ConfigError, EvaluationError
from experiments.benchmark import prepare_benchmark
from tests.domain_helpers import execution, snapshot


ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "experiments/configs/v0_1_fixed_prompt.yaml"


class BenchmarkStartupTests(unittest.TestCase):
    def config_file(self, directory, **changes):
        data = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
        for section, values in changes.items():
            data[section].update(values)
        path = Path(directory) / "config.yaml"
        path.write_text(yaml.safe_dump(data), encoding="utf-8")
        return path

    def cli(self, *args, cwd=ROOT):
        return subprocess.run([sys.executable, str(ROOT / "main.py"), *map(str, args)], cwd=cwd,
                              capture_output=True, encoding="utf-8")

    def test_startup_builds_only_benchmark_and_evaluator(self):
        prepared = prepare_benchmark(CONFIG)
        self.assertEqual(set(prepared.built_plugins.instances), {"benchmark", "evaluator"})
        self.assertEqual(len(prepared.execution_tasks), 4)
        self.assertEqual(len(prepared.similar_tasks), 4)
        self.assertEqual(len(prepared.regression_tasks), 4)
        data = prepared.as_dict()
        self.assertEqual(data["dataset_version"], "toy.v0.1.0")
        self.assertNotIn("eval_spec_ref", json.dumps(data))
        self.assertNotIn("private_parameters", json.dumps(data))

    def test_task_selection_preserves_benchmark_order(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self.config_file(directory, benchmark={"task_ids": ["dev-004", "dev-001"]})
            prepared = prepare_benchmark(config)
            self.assertEqual([t.task_id for t in prepared.execution_tasks], ["dev-001", "dev-004"])
            self.assertEqual(len(prepared.similar_tasks), 4)
            with self.assertRaises(EvaluationError):
                prepared.score(execution().replace(task_id="dev-002"), operation_id="eval-1")

    def test_unknown_selected_task_and_bad_real_groups_fail_setup(self):
        cases = [{"benchmark": {"task_ids": ["absent"]}},
                 {"benchmark": {"task_ids": ["test-001"]}},
                 {"validation": {"group_field": "unknown"}},
                 {"validation": {"similar_group": "other"}}]
        with tempfile.TemporaryDirectory() as directory:
            for changes in cases:
                with self.subTest(changes=changes), self.assertRaises(ConfigError):
                    prepare_benchmark(self.config_file(directory, **changes))

    def test_final_test_selects_only_test_and_requires_evolution_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            frozen = Path(directory) / "snapshot.json"
            frozen.write_text(snapshot().model_dump_json(), encoding="utf-8")
            data = yaml.safe_load((ROOT / "experiments/configs/v0_1_no_evolution.yaml").read_text(encoding="utf-8"))
            data["benchmark"]["execution_split"] = "test"
            data["agent"].update(initial_prompt=None, initial_snapshot_ref=str(frozen))
            config = Path(directory) / "test.yaml"
            config.write_text(yaml.safe_dump(data), encoding="utf-8")
            prepared = prepare_benchmark(config)
            self.assertEqual([t.task_id for t in prepared.execution_tasks], [f"test-{i:03d}" for i in range(1, 5)])
            self.assertTrue(prepared.score(execution().replace(task_id="test-001", output={"answer": 72}), operation_id="eval-1").success)
            # This command checks config, not Snapshot contents or model execution.
            data["evolution"].update(mode="fixed")
            config.write_text(yaml.safe_dump(data), encoding="utf-8")
            with self.assertRaises(ConfigError):
                prepare_benchmark(config)

    def test_inspection_from_other_working_directory_exports_only_public_tasks(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "benchmark.json"
            result = self.cli("inspect-benchmark", CONFIG, "--output", output, cwd=directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(data["status"], "benchmark_ready")
            self.assertEqual(len(data["index"]["splits"]["validation"]), 8)
            self.assertNotIn("private_parameters", json.dumps(data))

    def test_saved_execution_scores_and_tracks_agent_version(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "execution.json"
            path.write_text(execution("candidate-v2").replace(output={"answer": 75}).model_dump_json(), encoding="utf-8")
            result = self.cli("score-execution", CONFIG, path, "--operation-id", "score-v2", cwd=directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(result.stdout)
            self.assertTrue(data["evaluation"]["success"])
            self.assertEqual(data["evaluation"]["agent_version_id"], "candidate-v2")
            self.assertEqual(data["evaluation"]["execution_operation_id"], "exec-candidate-v2")
            self.assertEqual(data["feedback"]["reason_code"], "correct")

    def test_wrong_answer_is_normal_scoring_and_does_not_disclose_expected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "execution.json"
            path.write_text(execution().replace(output={"answer": 74}).model_dump_json(), encoding="utf-8")
            result = self.cli("score-execution", CONFIG, path)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(result.stdout)
            self.assertEqual(data["evaluation"]["score"], 0)
            self.assertEqual(data["feedback"]["reason_code"], "incorrect_answer")
            self.assertNotIn("75", result.stdout)

    def test_scoring_input_error_is_clear_and_nonzero(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "execution.json"
            for contents in ["{", '{"task_id":"dev-001","task_id":"test-001"}', '{"value":NaN}',
                             execution().replace(task_id="test-001").model_dump_json()]:
                path.write_text(contents, encoding="utf-8")
                result = self.cli("score-execution", CONFIG, path)
                self.assertEqual(result.returncode, 2)
                self.assertNotIn("Traceback", result.stderr)
                self.assertEqual(result.stdout, "")

    def test_output_cannot_overwrite_execution_or_packaged_dataset(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "execution.json"
            contents = execution().replace(output={"answer": 75}).model_dump_json()
            path.write_text(contents, encoding="utf-8")
            result = self.cli("score-execution", CONFIG, path, "--output", path)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(path.read_text(encoding="utf-8"), contents)
            for name in ("tasks.json", "scoring_specs.json"):
                data_path = ROOT / "plugins/benchmarks/toy_tasks" / name
                result = self.cli("inspect-benchmark", CONFIG, "--output", data_path)
                self.assertEqual(result.returncode, 2)
                self.assertIn("must not overwrite", result.stderr)
