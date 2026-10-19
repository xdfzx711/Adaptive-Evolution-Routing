import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import yaml
from pydantic import ValidationError

from aer_core.schemas.assessment import TaskExecutionRecord
from aer_core.schemas.references import ArtifactRef
from experiments.bootstrap import assemble_experiment
from experiments.execution import prepare_execution
from tests.execution.helpers import RecordingModel


ROOT = Path(__file__).resolve().parents[2]


class ExecutionStartupTests(unittest.TestCase):
    def config(self, directory, single=False, **changes):
        data = yaml.safe_load((ROOT / "experiments/configs/v0_1_no_evolution.yaml").read_text(encoding="utf-8"))
        data["storage"]["root"] = str(Path(directory) / "runs")
        if single:
            data["agent"].update(plugin="single_call_agent", max_steps=1)
        for section, values in changes.items():
            data[section].update(values)
        path = Path(directory) / ("single.yaml" if single else "react.yaml")
        path.write_text(yaml.safe_dump(data), encoding="utf-8")
        return path

    def cli(self, *args, cwd=ROOT):
        return subprocess.run([sys.executable, str(ROOT / "main.py"), *map(str, args)], cwd=cwd,
                              capture_output=True, encoding="utf-8")

    def test_config_switches_real_agents_without_changing_task_or_evaluator(self):
        with tempfile.TemporaryDirectory() as directory:
            for single in (False, True):
                with self.subTest(single=single):
                    prepared = prepare_execution(self.config(directory, single=single), run_id="single" if single else "react")
                    record = prepared.run_task("dev-001")
                    self.assertTrue(record.evaluation.success)
                    self.assertEqual(record.execution.usage.model_calls, 1 if single else 2)
                    self.assertEqual(record.execution.usage.tool_calls, 0 if single else 1)
                    self.assertEqual(record.diagnosis.reason_code, "no_failure")
                    self.assertEqual(TaskExecutionRecord.model_validate_json(record.model_dump_json()), record)
                    self.assertEqual(prepared.storage.version_store.stable().version_id, record.execution.agent_version_id)
                    plugins = set(prepared.built_plugins.instances)
                    self.assertNotIn("router", plugins)
                    self.assertEqual("tools" in plugins, not single)

    def test_disabled_real_assembly_injects_providers_without_persistent_store(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self.config(directory)
            assembled = assemble_experiment(config)
            self.assertEqual(set(assembled.built_plugins.instances), {"model", "tools", "agent"})
            self.assertEqual(assembled.plugins.agent.snapshot().agent_plugin_id, "react_agent")
            self.assertFalse((Path(directory) / "runs").exists())

    def test_records_persist_and_reopened_run_keeps_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self.config(directory)
            first = prepare_execution(config, run_id="persistent")
            record = first.run_task("dev-001")
            payload = first.save_record(record)
            again = prepare_execution(config, run_id="persistent")
            ref = ArtifactRef.model_validate(payload["record_ref"])
            saved = TaskExecutionRecord.model_validate_json(again.storage.artifact_store.get(ref))
            self.assertEqual(saved, record)
            second = again.run_task("dev-001")
            self.assertNotEqual(second.episode_id, record.episode_id)
            self.assertEqual(second.execution.output, record.execution.output)
            self.assertEqual([e.kind for e in second.execution.trajectory], [e.kind for e in record.execution.trajectory])
            self.assertEqual(second.execution.agent_version_id, record.execution.agent_version_id)
            self.assertEqual(again.storage.experience_store.events(), ())

    def test_step_limit_is_scored_and_diagnosed(self):
        with tempfile.TemporaryDirectory() as directory:
            prepared = prepare_execution(self.config(directory, agent={"max_steps": 1}))
            record = prepared.run_task("dev-001")
            self.assertEqual(record.execution.status, "step_limit")
            self.assertEqual(record.evaluation.score, 0)
            self.assertEqual(record.diagnosis.reason_code, "step_limit")
            self.assertEqual(record.execution.usage.tool_calls, 1)

    def test_infrastructure_failure_has_no_fabricated_evaluation_or_diagnosis(self):
        def crash(request, ctx):
            raise RuntimeError("provider unavailable")
        with tempfile.TemporaryDirectory() as directory:
            prepared = prepare_execution(self.config(directory))
            prepared.agent.model = RecordingModel(crash)
            record = prepared.run_task("dev-001")
            self.assertEqual(record.execution.status, "error")
            self.assertIsNone(record.evaluation)
            self.assertIsNone(record.feedback)
            self.assertIsNone(record.diagnosis)
            self.assertIsNone(record.execution.usage.total_tokens)
            self.assertEqual(record.execution.usage.model_calls, 1)
            self.assertEqual(prepared.save_record(record)["record"]["execution"]["error"]["code"], "execution_error")

    def test_diagnoser_failure_preserves_actual_score(self):
        class BrokenDiagnoser:
            def diagnose(self, task, result, feedback, run_ctx):
                raise RuntimeError("diagnosis unavailable")
        with tempfile.TemporaryDirectory() as directory:
            prepared = replace(prepare_execution(self.config(directory)), diagnoser=BrokenDiagnoser())
            record = prepared.run_task("dev-001")
            self.assertTrue(record.evaluation.success)
            self.assertIsNone(record.diagnosis)
            self.assertEqual(record.assessment_error.code, "diagnosis_error")

    def test_evaluator_failure_is_distinct_from_wrong_answer(self):
        class BrokenEvaluator:
            def evaluate(self, task, result, spec, run_ctx):
                raise RuntimeError("scoring unavailable")
        with tempfile.TemporaryDirectory() as directory:
            prepared = prepare_execution(self.config(directory))
            prepared = replace(prepared, benchmark=replace(prepared.benchmark, evaluator=BrokenEvaluator()))
            record = prepared.run_task("dev-001")
            self.assertEqual(record.execution.status, "completed")
            self.assertIsNone(record.evaluation)
            self.assertIsNone(record.diagnosis)
            self.assertEqual(record.assessment_error.code, "evaluation_error")

    def test_shipped_single_call_template_executes_from_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            data = yaml.safe_load((ROOT / "experiments/configs/v0_1_single_call.yaml").read_text(encoding="utf-8"))
            data["storage"]["root"] = str(Path(directory) / "runs")
            config = Path(directory) / "single.yaml"
            config.write_text(yaml.safe_dump(data), encoding="utf-8")
            result = self.cli("execute-task", config, "--task-id", "dev-001", cwd=directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            record = json.loads(result.stdout)["record"]
            self.assertEqual(record["execution"]["usage"]["model_calls"], 1)
            self.assertEqual(record["execution"]["usage"]["tool_calls"], 0)
            self.assertTrue(record["evaluation"]["success"])

    def test_record_rejects_wrong_version_score_and_missing_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            record = prepare_execution(self.config(directory)).run_task("dev-001")
            with self.assertRaises(ValidationError):
                record.replace(evaluation=record.evaluation.replace(agent_version_id="wrong"))
            with self.assertRaises(ValidationError):
                record.replace(diagnosis=record.diagnosis.replace(evidence_step_ids=[999]))

    def test_cli_executes_with_fixed_evolution_config_without_running_router(self):
        with tempfile.TemporaryDirectory() as directory:
            data = yaml.safe_load((ROOT / "experiments/configs/v0_1_fixed_prompt.yaml").read_text(encoding="utf-8"))
            data["storage"]["root"] = str(Path(directory) / "runs")
            config = Path(directory) / "fixed.yaml"
            config.write_text(yaml.safe_dump(data), encoding="utf-8")
            output = Path(directory) / "result.json"
            result = self.cli("execute-task", config, "--task-id", "dev-001", "--run-id", "cli", "--output", output, cwd=directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "executed")
            self.assertTrue(payload["record"]["evaluation"]["success"])
            self.assertEqual(payload["record"]["execution"]["usage"]["model_calls"], 2)
            self.assertTrue((Path(payload["run_root"]) / "run_manifest.json").is_file())

    def test_cli_task_format_failure_is_normal_result_and_invalid_selection_is_error(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self.config(directory, limits={"max_output_tokens_per_call": 1})
            result = self.cli("execute-task", config, "--task-id", "dev-001")
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["record"]["evaluation"]["score"], 0)
            self.assertEqual(payload["record"]["diagnosis"]["reason_code"], "invalid_output_format")
            invalid = self.cli("execute-task", config, "--task-id", "test-001")
            self.assertEqual(invalid.returncode, 2)
            self.assertIn("outside", invalid.stderr)
            self.assertNotIn("Traceback", invalid.stderr)

    def test_cli_timeout_returns_error_record_and_nonzero_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self.config(directory, limits={"model_call_timeout_seconds": 1e-12})
            result = self.cli("execute-task", config, "--task-id", "dev-001")
            self.assertEqual(result.returncode, 2, result.stderr)
            payload = json.loads(result.stdout)
            self.assertIsNone(payload["record"]["evaluation"])
            self.assertEqual(payload["record"]["execution"]["error"]["code"], "operation_timeout")
            self.assertEqual(payload["record"]["execution"]["usage"]["model_calls"], 1)

    def test_cli_does_not_overwrite_managed_manifest_or_config(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self.config(directory)
            contents = config.read_text(encoding="utf-8")
            result = self.cli("execute-task", config, "--task-id", "dev-001", "--run-id", "guard", "--output", config)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(config.read_text(encoding="utf-8"), contents)
            manifest = Path(directory) / "runs/guard/run_manifest.json"
            prior = manifest.read_bytes()
            result = self.cli("execute-task", config, "--task-id", "dev-001", "--run-id", "guard", "--output", manifest)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(manifest.read_bytes(), prior)

    def test_frozen_snapshot_executes_final_test_without_learning(self):
        with tempfile.TemporaryDirectory() as directory:
            initial_config = self.config(directory)
            prepared = prepare_execution(initial_config, run_id="development")
            frozen = prepared.storage.run_root / prepared.storage.initial_snapshot_ref.uri
            test_config = self.config(directory, benchmark={"execution_split": "test"},
                agent={"initial_prompt": None, "initial_snapshot_ref": str(frozen)})
            final = prepare_execution(test_config, run_id="final")
            record = final.run_task("test-001")
            self.assertTrue(record.evaluation.success)
            self.assertEqual(record.execution.agent_version_id, prepared.storage.version_store.stable().version_id)
            self.assertEqual(final.storage.experience_store.events(), ())
            self.assertIsNone(record.diagnosis)
