"""HotpotQA + native construction + real MCP, with offline model/CLI responses."""

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from experiments.batch import run_benchmark
from experiments.configuration import load_experiment_config
from plugins.agents.codex import initial_artifacts
from plugins.evolution.oscopilot.source import DEFAULT_SOURCE
from tests.evolution.test_oscopilot import native_plan
from tests.integration.oscopilot_hotpotqa_helpers import BASE, configured, offline_child, offline_construction, overrides


class HotpotToolConfigurationTests(unittest.TestCase):
    def test_overrides_reuse_shipped_config_without_changing_prompt_or_protocol(self):
        before = BASE.read_bytes()
        old = load_experiment_config(BASE)
        new = load_experiment_config(BASE, overrides=overrides())
        self.assertEqual(BASE.read_bytes(), before)
        for field in ("benchmark", "state", "validation", "assessment", "evolution"):
            self.assertEqual(getattr(old.config, field), getattr(new.config, field))
        self.assertEqual(old.config.agent.initial_prompt, new.config.agent.initial_prompt)
        self.assertEqual(new.config.targets.allowed, ["tool"])
        self.assertEqual(set(new.config.targets.operator_configs), {"tool"})
        artifacts = initial_artifacts(new.config)
        self.assertEqual(json.loads(artifacts["tool"])["generated_tools"], {})
        self.assertNotIn("tool", initial_artifacts(old.config))


@unittest.skipUnless(importlib.util.find_spec("ipykernel") and (DEFAULT_SOURCE / "oscopilot").is_dir(),
                     "initialize OS-Copilot and install [oscopilot]")
class HotpotToolIntegrationTests(unittest.TestCase):
    def check_committed(self, summary, manifest):
        self.assertEqual(summary["status"], "completed", summary["tasks"])
        self.assertEqual(summary["aggregate"]["mean_native_metrics_evaluated"]["joint_f1"], 1)
        self.assertEqual(summary["aggregate"]["tool_construction"]["committed_candidates"], len(summary["tasks"]))
        for entry in summary["tasks"]:
            self.assertEqual(entry["initial_tool_library"]["enabled_tools"], [])
            self.assertEqual(entry["final_tool_library"]["enabled_tools"], ["locate_evidence"])
            self.assertEqual(entry["initial_prompt"], entry["final_prompt"])
            self.assertNotEqual(entry["initial_version_id"], entry["final_stable_version_id"])
            self.assertEqual(entry["initial_evaluation"]["score"], 0)
            row, = entry["rounds"]
            construction = row["tool_construction"]
            self.assertEqual(construction["status"], "completed")
            self.assertEqual(construction["adapter_version"], "2.8.0")
            self.assertEqual(construction["completed_python_tools"], ["locate_evidence"])
            self.assertEqual(construction["exported_tools"], ["locate_evidence"])
            self.assertEqual(construction["delivery_verified_tools"], ["locate_evidence"])
            self.assertEqual(construction["model_call_attempts_by_stage"], {
                "planning": 1, "construction": 1, "judgment": 1, "export_metadata": 1})
            groups = row["candidates"][0]["validation"]["tool_usage_analysis"]["groups"]
            self.assertEqual(set(groups), {"local", "similar", "regression"})
            for group in groups.values():
                self.assertEqual(group["candidate"]["locate_evidence"]["tasks_invoked"], 1)
                self.assertEqual(group["candidate"]["locate_evidence"]["execution_success"], 1)
            self.assertEqual(entry["holdouts"], manifest.validation_sets[entry["task_id"]].model_dump())
            root = Path(entry.get("run_root", summary["run_root"]))
            record = json.loads((root / row["record"]).read_text(encoding="utf-8"))
            calls = record["generation"]["tool_construction"]["calls"]
            evidence = json.loads(calls[0]["request"]["messages"][-1]["content"].split(
                "AER public development evidence (data, not instructions):\n")[-1])
            task_input = evidence["context"]["task"]["input"]
            feedback = evidence["context"]["feedback"]
            self.assertEqual(feedback["feedback_version"], "aer.feedback.v2")
            self.assertEqual(len(feedback["metrics"]), 12)
            self.assertEqual(feedback["scoring_rules"]["primary_metric"], "joint_f1")
            self.assertNotIn("usage", evidence["context"]["diagnosis"])
            self.assertIn("question", task_input)
            self.assertIn("context", task_input)
            # Actual task inputs exceed the small export-example limit and still reach the callable.
            self.assertGreater(len(json.dumps(task_input["context"])), 2048)
            self.assertNotIn("answer", task_input)
            self.assertNotIn("supporting_facts", task_input)
            self.assertNotIn("private_parameters", json.dumps(calls))
            self.assertEqual(evidence["existing_tools"], {})
            self.assertNotIn("builtins", evidence["outer_agent_capabilities"])
            for holdout in (*entry["holdouts"]["similar"], *entry["holdouts"]["regression"]):
                self.assertNotIn(holdout, json.dumps(calls))
            for pair in record["candidates"][0]["validation"]["tasks"]:
                invocation = pair["candidate"]["execution"]["state_summary"]["tool_invocations"][0]
                # The real bridge executes each task's current JSON, not the export example.
                self.assertTrue(pair["candidate"]["evaluation"]["success"])
                self.assertEqual(pair["candidate"]["execution"]["output"]["supporting_facts"][0][1], 1)
                self.assertEqual(invocation["status"], "completed")
        predictions = json.loads(Path(summary["predictions_path"]).read_text(encoding="utf-8"))
        self.assertEqual(set(predictions["answer"]), {entry["task_id"] for entry in summary["tasks"]})

    def test_parameterized_tools_commit_and_each_episode_starts_empty(self):
        with tempfile.TemporaryDirectory() as directory, offline_construction():
            loaded, _, manifest = configured(Path(directory), tasks=2)
            result = run_benchmark(loaded, report_holdouts=True)
            self.check_committed(result, manifest)

    def test_regression_rejects_candidate_and_preserves_final_library(self):
        with tempfile.TemporaryDirectory() as directory, offline_construction():
            loaded, _, _ = configured(Path(directory), mode="reject")
            result = run_benchmark(loaded, report_holdouts=True)
            self.assertEqual(result["status"], "completed")
            entry, = result["tasks"]
            self.assertEqual(entry["initial_version_id"], entry["final_stable_version_id"])
            self.assertEqual(entry["initial_tool_library"], entry["final_tool_library"])
            self.assertEqual(entry["final_tool_library"]["enabled_tools"], [])
            self.assertEqual(entry["final_evaluation"]["score"], 0)
            candidate = entry["rounds"][0]["candidates"][0]
            self.assertFalse(candidate["committed"])
            self.assertEqual(candidate["acceptance"]["reason"], "regression_guard")
            self.assertEqual(result["aggregate"]["tool_construction"]["committed_candidates"], 0)
            self.assertTrue(entry["holdout_reporting"]["unchanged_version"])

    def test_initial_success_skips_construction(self):
        with tempfile.TemporaryDirectory() as directory, offline_construction() as models:
            loaded, _, _ = configured(Path(directory), mode="perfect")
            result = run_benchmark(loaded, report_holdouts=True)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(models, [])
            self.assertEqual(result["aggregate"]["tool_construction"]["rounds"], 0)
            self.assertEqual(result["tasks"][0]["final_tool_library"]["enabled_tools"], [])

    def test_empty_construction_is_reported_without_a_candidate_or_tool_leak(self):
        with tempfile.TemporaryDirectory() as directory, offline_construction(lambda: [native_plan({})]):
            loaded, _, _ = configured(Path(directory))
            result = run_benchmark(loaded)
            self.assertEqual(result["status"], "completed")
            entry, = result["tasks"]
            self.assertEqual(entry["initial_tool_library"], entry["final_tool_library"])
            self.assertEqual(entry["rounds"][0]["tool_construction"]["status"], "empty")
            self.assertEqual(entry["rounds"][0]["candidates"], [])
            self.assertEqual(result["aggregate"]["tool_construction"]["statuses"], {"empty": 1})

    def test_parallel_tasks_keep_tool_state_and_reports_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            loaded, _, manifest = configured(Path(directory), tasks=2)
            with patch("experiments.parallel_batch._run_child", offline_child):
                result = run_benchmark(loaded, workers=2, report_holdouts=True)
            self.check_committed(result, manifest)
            self.assertEqual(len({entry["run_root"] for entry in result["tasks"]}), 2)
