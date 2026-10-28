import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import yaml

from aer_core.errors import ConfigError, ContractError
from aer_core.schemas.task import TaskIdentity
from experiments.configuration import load_experiment_config
from experiments.evolution import prepare_evolution
from experiments.execution import prepare_execution
from experiments.optimization import ConfiguredModelFactory, IsolatedCandidateTrials
from experiments.orchestration import prepare_orchestrator
from experiments.bootstrap import create_registry
from experiments.reporting import summarize_saved_run
from plugins.providers.scripted import ScriptedModel
from tests.execution.helpers import RecordingModel, context, response


ROOT = Path(__file__).resolve().parents[2]


def configured(root, *, mode="fixed", reflection=None):
    data = yaml.safe_load((ROOT / "experiments/configs/v0_1_fixed_prompt.yaml").read_text(encoding="utf-8"))
    data["storage"]["root"] = str(root / "runs")
    data["agent"].update(plugin="single_call_agent", max_steps=1, initial_prompt="BAD seed instructions")
    data["targets"]["default_operators"]["prompt"] = "gepa_prompt_evolver"
    data["targets"]["operator_configs"] = {"prompt": {"max_metric_calls": 6, "max_iterations": 1,
        "reflection_model": reflection or data["providers"]["model"]}}
    data["evolution"]["mode"] = mode
    data["limits"]["max_evolution_rounds"] = 1
    if mode == "llm":
        data["routing"].update(plugin="llm_router", fixed_target=None)
    path = root / "config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


class GEPAConfigurationTests(unittest.TestCase):
    def test_cli_output_cannot_overwrite_reflection_dotenv(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = root / "reflection.env"
            env.write_text("GEPA_REFLECTION_MODEL=fixture\n", encoding="utf-8")
            path = configured(root, reflection={"plugin": "qwen_model", "model_id": "${GEPA_REFLECTION_MODEL}",
                "config": {"dotenv_path": "reflection.env", "api_key_env": "GEPA_REFLECTION_API_KEY",
                           "base_url_env": "GEPA_REFLECTION_BASE_URL"}})
            before = env.read_bytes()
            result = subprocess.run([sys.executable, str(ROOT / "main.py"), "validate-config", str(path),
                "--output", str(env)], encoding="utf-8", capture_output=True)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(env.read_bytes(), before)

    def test_nested_reflection_provider_configuration_is_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            path = configured(Path(directory), reflection={"plugin": "missing", "model_id": "x", "config": {}})
            with self.assertRaisesRegex(ConfigError, "reflection_model.plugin"):
                load_experiment_config(path)

    def test_reflection_environment_and_provider_are_independent(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "GEPA_REFLECTION_MODEL": "isolated-reflector", "GEPA_REFLECTION_API_KEY": "test-key",
            "GEPA_REFLECTION_BASE_URL": "https://reflection.example/v1"}):
            root = Path(directory)
            path = configured(root, reflection={"plugin": "qwen_model", "model_id": "${GEPA_REFLECTION_MODEL}", "config": {
                "api_key_env": "GEPA_REFLECTION_API_KEY", "base_url_env": "GEPA_REFLECTION_BASE_URL"}})
            loaded = load_experiment_config(path)
            original = loaded.config.providers.model
            settings = loaded.config.targets.operator_configs["prompt"]["reflection_model"]
            from aer_core.config.models import ModelProviderSettings
            model = ConfiguredModelFactory(loaded, create_registry()).create(ModelProviderSettings.model_validate(settings))
            self.assertEqual(model.model_id, "isolated-reflector")
            self.assertEqual(loaded.config.providers.model, original)
            self.assertEqual(original.model_id, "scripted-v0.1")
            self.assertNotIn("test-key", json.dumps(loaded.config.model_dump(mode="json")))


@unittest.skipUnless(importlib.util.find_spec("gepa"), "install the [gepa] extra")
class GEPAStartupTests(unittest.TestCase):
    def test_codex_trials_inherit_snapshot_execution_model_and_effort(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(ConfiguredModelFactory, "create",
                return_value=RecordingModel(lambda *_: response("IMPROVED reusable instructions"))):
            root = Path(directory)
            path = configured(root)
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            data["agent"] = {"plugin": "codex_cli_agent", "initial_prompt": "BAD seed instructions",
                "initial_snapshot_ref": None, "config": {
                    "command": [sys.executable, str(ROOT / "tests/execution/fake_codex.py"), "--fixture-mode", "local-failure"],
                    "workspace_root": str(root / "outer-workspaces"),
                    "initial_reasoning_effort": "medium", "allowed_reasoning_efforts": {"gpt-6.1-sol": ["medium", "high"]}}}
            data["validation"].update(acceptance_policy="weighted_improvement", max_regression_drop=None)
            path.write_text(yaml.safe_dump(data), encoding="utf-8")
            startup = prepare_orchestrator(path)
            result = startup.run_task("dev-001")
            self.assertIsNone(result.error)
            self.assertEqual(result.stop_reason, "task_succeeded")
            record = json.loads(next(startup.execution.storage.run_root.glob("rounds/*/round-001/record.json")).read_text(encoding="utf-8"))
            internal = record["generation"]["internal_optimization"]
            for trial in internal["trials"]:
                state = trial["execution"]["state_summary"]
                self.assertEqual(state["execution_model"], "gpt-6.1-sol")
                self.assertEqual(state["execution_reasoning_effort"], "medium")
            candidate = record["candidates"][0]
            self.assertEqual(set(candidate["components"]), {"prompt"})
            self.assertEqual(candidate["acceptance"]["delta_score"], 0)
            self.assertEqual(candidate["acceptance"]["reward"], 0.5)
            for pair in candidate["validation"]["tasks"]:
                for side in ("baseline", "candidate"):
                    self.assertEqual(pair[side]["execution"]["state_summary"]["execution_model"], "gpt-6.1-sol")

    def test_real_gepa_isolated_trials_and_outer_commit_use_existing_interfaces(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            path = configured(root)
            reflection = RecordingModel(lambda *_: response("IMPROVED reusable instructions"))
            stack.enter_context(patch.object(ConfiguredModelFactory, "create", return_value=reflection))
            original = ScriptedModel.generate
            called_models = []

            def generate(model, request, run_ctx):
                called_models.append((model, run_ctx))
                return original(model, request, run_ctx) if request.messages[0].content.startswith("IMPROVED") else response('{"answer":-999}')

            stack.enter_context(patch.object(ScriptedModel, "generate", generate))
            startup = prepare_orchestrator(path)
            task = startup.prepare_task("dev-001")
            initial = task.execution.storage.version_store.stable()
            result = task.run()
            self.assertIsNone(result.error)
            self.assertEqual(result.stop_reason, "task_succeeded")
            self.assertEqual(result.evolution_rounds_used, 1)
            self.assertNotEqual(result.final_stable_version_id, initial.version_id)
            self.assertEqual(task.execution.storage.version_store.load(initial.version_id), initial)
            events = task.execution.storage.experience_store.events()
            self.assertEqual(sum(event.record_type == "attempt_finished" for event in events), 1)
            self.assertEqual(sum(event.record_type == "version_committed" for event in events), 1)
            run_root = task.execution.storage.run_root
            files = list(run_root.glob("rounds/*/round-001/record.json"))
            self.assertEqual(len(files), 1)
            data = json.loads(files[0].read_text(encoding="utf-8"))
            internal = data["generation"]["internal_optimization"]
            self.assertEqual(internal["status"], "completed")
            self.assertEqual(internal["config"]["samples_per_evaluation"], 3)
            self.assertEqual(internal["config"]["score_aggregation"], "mean")
            self.assertEqual(len(internal["trials"]), 6)
            self.assertEqual([item["sample_count"] for item in internal["decisions"]
                              if item["event"] == "sampled_evaluation"], [3, 3])
            self.assertTrue(internal["reflections"])
            self.assertTrue(all(trial["task"]["task_id"] == "dev-001" for trial in internal["trials"]))
            self.assertEqual(internal["candidates"][-1]["prompt"], "IMPROVED reusable instructions")
            self.assertIn("trace", internal["trials"][0]["execution"])
            internal_calls = [model for model, ctx in called_models if ctx.run_id.startswith("trial-run-")]
            self.assertEqual(len(internal_calls), len(internal["trials"]))
            self.assertTrue(all(model is not task.execution.agent.model for model in internal_calls))
            self.assertEqual(len(set(id(model) for model in internal_calls)), len(internal_calls))
            self.assertEqual(len(reflection.requests), len(internal["reflections"]))
            self.assertNotIn("scoring_specs", json.dumps(internal))
            text = (run_root / "evolution.txt").read_text(encoding="utf-8")
            self.assertIn("Evolver 内部优化", text)
            self.assertIn("内部试跑", text)
            self.assertIn("内部反思", text)
            self.assertIn("采样汇总", text)
            self.assertIn("复用评测", text)
            self.assertIn("固定采样 3 次，取平均分", text)
            self.assertNotIn("报告异常", text)
            self.assertFalse(list(run_root.rglob("gepa_state.bin")))
            self.assertEqual(len(list(files[0].parent.glob("*.json"))), 1)
            saved = root / "result.json"
            saved.write_text(json.dumps(startup.save_record(result)), encoding="utf-8")
            # Receipts also render after the temporary trial workspaces are gone.
            (run_root / "evolution.txt").unlink()
            summarize_saved_run(saved)
            self.assertIn("Evolver 内部优化", (run_root / "evolution.txt").read_text(encoding="utf-8"))

    def test_trial_service_has_no_outer_cache_state_or_validation_access(self):
        with tempfile.TemporaryDirectory() as directory:
            path = configured(Path(directory))
            prepared = prepare_execution(path)
            stable = prepared.storage.version_store.stable()
            service = IsolatedCandidateTrials(prepared.benchmark.loaded_config, create_registry(),
                prepared.storage.artifact_store, prepared.benchmark.execution_tasks)
            current = prepared.benchmark.execution_tasks[0].identity
            before = {file.relative_to(prepared.storage.run_root): file.read_bytes()
                      for file in prepared.storage.run_root.rglob("*") if file.is_file()}
            observed = service.evaluate(current, current, stable, "Only trial prompt", context())
            self.assertIsNone(observed.error)
            self.assertIsNotNone(observed.feedback)
            self.assertEqual(prepared.agent.snapshot(), stable)
            after = {file.relative_to(prepared.storage.run_root): file.read_bytes()
                     for file in prepared.storage.run_root.rglob("*") if file.is_file()}
            self.assertEqual(before, after)
            for name in ("similar-001", "test-001", "dev-002"):
                with self.assertRaises(ContractError):
                    service.tasks(current, (name,))
            with self.assertRaises(ContractError):
                service.evaluate(current, TaskIdentity(benchmark_id="foreign", task_id=current.task_id), stable,
                                 "prompt", context())

    def test_private_evaluator_prose_and_metrics_do_not_enter_trial_feedback(self):
        from plugins.evaluators.deterministic import DeterministicEvaluator
        original = DeterministicEvaluator.evaluate
        def private_feedback(self, task, result, spec, run_ctx):
            return original(self, task, result, spec, run_ctx).replace(
                feedback="PRIVATE_SCORER_CANARY", metrics={"PRIVATE_SCORER_CANARY": 1.0})
        with tempfile.TemporaryDirectory() as directory, patch.object(DeterministicEvaluator, "evaluate",
                private_feedback):
            prepared = prepare_execution(configured(Path(directory)))
            service = IsolatedCandidateTrials(prepared.benchmark.loaded_config, create_registry(),
                prepared.storage.artifact_store, prepared.benchmark.execution_tasks)
            current = prepared.benchmark.execution_tasks[0].identity
            observed = service.evaluate(current, current, prepared.storage.version_store.stable(), "prompt", context())
            self.assertIsNone(observed.error)
            self.assertNotIn("PRIVATE_SCORER_CANARY", observed.model_dump_json())

    def test_router_configuration_does_not_change_the_gepa_plugin(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(ConfiguredModelFactory, "create",
                return_value=RecordingModel(lambda *_: response("IMPROVED reusable instructions"))):
            root = Path(directory)
            for mode in ("fixed", "llm"):
                path = configured(root, mode=mode)
                generation = prepare_evolution(path, run_id="gepa-" + mode)
                self.assertEqual(generation.resolver.resolve("prompt").plugin_id, "gepa_prompt_evolver")
