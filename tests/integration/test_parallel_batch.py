"""Spawn-process tests use deterministic local providers; no external model calls."""

from datetime import datetime
from contextlib import ExitStack
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import yaml

from experiments.batch import run_benchmark
from experiments.configuration import load_experiment_config
from experiments.parallel_batch import _run_child
from tests.benchmarks.hotpotqa_helpers import ROOT, config_file


def overlapping_child(loaded, task_id, run_id, console, report_holdouts):
    # A barrier proves that three real spawn workers run simultaneously.
    barrier = Path(loaded.config.storage.root) / "probe"
    barrier.mkdir(parents=True, exist_ok=True)
    (barrier / run_id).touch()
    deadline = time.monotonic() + 30
    while len(list(barrier.glob("task-*"))) < 3:
        if time.monotonic() > deadline:
            raise RuntimeError("three workers did not overlap")
        time.sleep(0.02)
    return _run_child(loaded, task_id, run_id, console, report_holdouts)


def failing_child(loaded, task_id, run_id, console, report_holdouts):
    if run_id == "task-0002":
        raise RuntimeError("injected worker error")
    return _run_child(loaded, task_id, run_id, console, report_holdouts)


def terminated_child(loaded, task_id, run_id, console, report_holdouts):
    if run_id == "task-0001":
        os._exit(17)
    return _run_child(loaded, task_id, run_id, console, report_holdouts)


def setup_error_child(loaded, task_id, run_id, console, report_holdouts):
    from aer_core.errors import ConfigError, ConfigIssue, PluginConfigurationError
    from aer_core.schemas.usage import ResourceUsage
    if run_id in ("task-0001", "task-0002"):
        error = (ConfigError(loaded.source_path, [ConfigIssue("fixture", "invalid setup")])
                 if run_id == "task-0001" else
                 PluginConfigurationError("fixture provider setup failed", stage="setup", usage=ResourceUsage.zero()))
        with patch("experiments.batch.run_benchmark", side_effect=error):
            return _run_child(loaded, task_id, run_id, console, report_holdouts)
    return _run_child(loaded, task_id, run_id, console, report_holdouts)


def local_gepa_child(loaded, task_id, run_id, console, report_holdouts):
    # Inject offline model responses inside the spawn worker, leaving the actual
    # GEPA search, HotpotQA evaluator, acceptance and storage path intact.
    from tests.integration.test_hotpotqa_baseline import HotpotGEPATests
    rows = []
    for name in ("train.json", "dev.json"):
        rows.extend(json.loads((loaded.source_path.parent / name).read_text(encoding="utf-8")))
    with ExitStack() as stack:
        HotpotGEPATests().model_patches(stack, rows)
        return _run_child(loaded, task_id, run_id, console, report_holdouts)


class ParallelBatchTests(unittest.TestCase):
    def test_cli_spawn_entrypoint_and_utf8_console(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.fixture(Path(directory))
            completed = subprocess.run([sys.executable, "-m", "experiments", "run-benchmark", str(path),
                "--run-id", "cli-smoke", "--workers", "3", "--limit", "3",
                "--set", "limits.max_evolution_rounds=0"], cwd=ROOT, capture_output=True,
                encoding="utf-8", timeout=60)
            self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
            self.assertIn("[开始]", completed.stderr)
            self.assertNotIn("\ufffd", completed.stdout + completed.stderr)
            summary = json.loads((Path(directory) / "runs/cli-smoke/baseline_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["aggregate"]["finished_tasks"], 3)
            self.assertEqual(summary["aggregate"]["error_tasks"], 0)

    def test_framework_setup_errors_cross_process_boundary_without_breaking_pool(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.fixture(Path(directory))
            with patch("experiments.parallel_batch._run_child", setup_error_child):
                result = run_benchmark(path, limit=4, workers=3, report_holdouts=True)
            self.assertEqual(result["aggregate"]["finished_tasks"], 4)
            self.assertEqual(result["aggregate"]["error_tasks"], 2)
            self.assertEqual([entry["status"] for entry in result["tasks"]],
                             ["error", "error", "completed", "completed"])
            for entry, kind in zip(result["tasks"], ("ConfigError", "PluginConfigurationError")):
                self.assertEqual(entry["error"]["type"], kind)
                saved = json.loads((Path(entry["run_root"]) / "worker_error.json").read_text(encoding="utf-8"))
                self.assertEqual(saved, entry["error"])
            self.assertEqual(result["tasks"][1]["usage"]["total_tokens"], 0)

    def fixture(self, root):
        data = yaml.safe_load((ROOT / "experiments/configs/v0_1_fixed_prompt.yaml").read_text(encoding="utf-8"))
        data["storage"]["root"] = str(root / "runs")
        # Step limit deliberately makes tasks fail, exercising all three evolution rounds.
        data["agent"]["max_steps"] = 1
        data["validation"].update(acceptance_policy="weighted_improvement",
                                  min_improvement=0.04, max_regression_drop=0.10)
        path = root / "parallel.yaml"
        path.write_text(yaml.safe_dump(data), encoding="utf-8")
        return path

    def test_three_processes_isolate_tasks_and_match_serial_metrics(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.fixture(Path(directory))
            serial = run_benchmark(path, run_id="serial", limit=4, report_holdouts=True)
            with patch("experiments.parallel_batch._run_child", overlapping_child):
                parallel = run_benchmark(path, run_id="parallel", limit=4, workers=3, report_holdouts=True)
            self.assertEqual(parallel["status"], "completed")
            self.assertEqual(parallel["aggregate"], serial["aggregate"])
            self.assertEqual(parallel["usage"]["model_calls"], serial["usage"]["model_calls"])
            self.assertEqual(parallel["usage"]["total_tokens"],
                             sum(entry["usage"]["total_tokens"] for entry in parallel["tasks"]))
            self.assertEqual(len({e["worker_pid"] for e in parallel["tasks"]}), 3)
            self.assertNotIn(os.getpid(), {e["worker_pid"] for e in parallel["tasks"]})
            self.assertEqual([e["task_id"] for e in parallel["tasks"]], parallel["requested_tasks"])
            roots = {e["run_root"] for e in parallel["tasks"]}
            self.assertEqual(len(roots), 4)
            self.assertTrue(any(len(entry["rounds"]) == 3 for entry in parallel["tasks"]))
            for entry, old in zip(parallel["tasks"], serial["tasks"]):
                self.assertEqual(entry["initial_prompt"], old["initial_prompt"])
                self.assertEqual(entry["final_prompt"], old["final_prompt"])
                self.assertEqual(len(entry["rounds"]), len(old["rounds"]))
                self.assertEqual(entry["stop_reason"], old["stop_reason"])
                child_root = Path(entry["run_root"])
                for row in entry["rounds"]:
                    self.assertTrue((child_root / row["record"]).is_file())
                self.assertGreaterEqual(datetime.fromisoformat(entry["finished_at"]),
                                        datetime.fromisoformat(entry["started_at"]))
                child = json.loads((child_root / "baseline_summary.json").read_text(encoding="utf-8"))
                self.assertEqual(child["requested_tasks"], [entry["task_id"]])
            protocol = json.loads((Path(parallel["run_root"]) / "baseline_protocol.json").read_text(encoding="utf-8"))
            self.assertEqual(protocol["workers"], 3)
            self.assertEqual(protocol["acceptance"]["min_improvement"], 0.04)
            self.assertEqual(protocol["acceptance"]["max_regression_drop"], 0.10)
            self.assertEqual(protocol["limits"]["max_evolution_rounds"], 3)

    def test_worker_error_is_recorded_and_other_tasks_continue(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.fixture(Path(directory))
            with patch("experiments.parallel_batch._run_child", failing_child):
                result = run_benchmark(path, limit=4, workers=3, report_holdouts=True)
            self.assertEqual(result["status"], "completed_with_errors")
            self.assertEqual(result["aggregate"]["finished_tasks"], 4)
            self.assertEqual(result["aggregate"]["error_tasks"], 1)
            self.assertEqual(result["aggregate"]["pending_tasks"], 0)
            self.assertIn("injected worker error", result["tasks"][1]["error"]["message"])
            self.assertFalse(result["usage"]["usage_complete"])
            self.assertIsNone(result["usage"]["total_tokens"])
            for group in ("similar", "regression"):
                count = len(result["tasks"][1]["holdouts"][group])
                self.assertGreater(count, 0)
                self.assertEqual(result["aggregate"]["holdouts_initial_vs_final"][group]["missing_final_count"], count)

    def test_terminated_process_accounts_for_queued_tasks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.fixture(Path(directory))
            with patch("experiments.parallel_batch._run_child", terminated_child):
                result = run_benchmark(path, limit=4, workers=3)
            self.assertEqual(result["status"], "completed_with_errors")
            self.assertEqual(result["aggregate"]["finished_tasks"], 4)
            self.assertEqual(result["aggregate"]["pending_tasks"], 0)

    def test_rejects_continual_and_invalid_worker_counts_before_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.fixture(Path(directory))
            for count in (0, -1, True, 1.5):
                with self.assertRaisesRegex(ValueError, "workers"):
                    run_benchmark(path, workers=count)
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            data["state"]["task_mode"] = "continual"
            path.write_text(yaml.safe_dump(data), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "independent"):
                run_benchmark(path, workers=3)
            self.assertFalse((Path(directory) / "runs").exists())

    def test_native_predictions_and_holdout_errors_are_aggregated(self):
        with tempfile.TemporaryDirectory() as directory:
            path, _, _ = config_file(Path(directory))
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            data["evolution"]["mode"] = "disabled"
            data["routing"].update(plugin=None, fixed_target=None)
            data["targets"].update(allowed=[], default_operators={}, operator_configs={})
            data["limits"]["max_evolution_rounds"] = 0
            path.write_text(yaml.safe_dump(data), encoding="utf-8")
            result = run_benchmark(path, workers=3, report_holdouts=True)
            # ScriptedModel cannot answer this HotpotQA input: errors stay explicit.
            self.assertEqual(result["status"], "completed_with_errors")
            self.assertEqual(result["aggregate"]["finished_tasks"], 2)
            self.assertTrue(Path(result["predictions_path"]).is_file())
            self.assertIn("holdouts_initial_vs_final", result["aggregate"])

    @unittest.skipUnless(importlib.util.find_spec("gepa"), "install [gepa]")
    def test_three_gepa_tasks_with_native_scoring_and_holdout_reporting(self):
        with tempfile.TemporaryDirectory() as directory:
            path, _, _ = config_file(Path(directory), tasks=3)
            loaded = load_experiment_config(path, overrides=[
                "limits.max_evolution_rounds=3", "evolution.stop_on_success=true",
                "validation.min_improvement=0.04", "validation.max_regression_drop=0.10"])
            with patch("experiments.parallel_batch._run_child", local_gepa_child):
                result = run_benchmark(loaded, workers=3, report_holdouts=True)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["aggregate"]["success_rate_evaluated"], 1)
            self.assertEqual(len(result["tasks"]), 3)
            predictions = json.loads(Path(result["predictions_path"]).read_text(encoding="utf-8"))
            self.assertEqual(set(predictions["answer"]), set(result["requested_tasks"]))
            for entry in result["tasks"]:
                self.assertEqual(entry["initial_prompt"], "BAD initial prompt")
                self.assertEqual(entry["stop_reason"], "task_succeeded")
                self.assertEqual(len(entry["rounds"]), 1)
                self.assertTrue(entry["rounds"][0]["candidates"][0]["committed"])
                self.assertEqual(entry["holdout_reporting"]["status"], "complete")


if __name__ == "__main__":
    unittest.main()
