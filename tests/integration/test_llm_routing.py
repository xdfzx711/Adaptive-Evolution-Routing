import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from aer_core.errors import ConfigError
from experiments.configuration import load_experiment_config
from experiments.orchestration import prepare_orchestrator
from experiments.report_export import export_saved_run
from plugins.agents.limits import ExecutionLimits
from plugins.providers.scripted import ScriptedModel
from tests.execution.helpers import MODEL, RecordingModel, response


ROOT = Path(__file__).resolve().parents[2]


class LLMRoutingIntegrationTests(unittest.TestCase):
    def config(self, root, *, rounds=1, cap=3, initial=1, single=False):
        data = yaml.safe_load((ROOT / "experiments/configs/v0_1_fixed_prompt.yaml").read_text(encoding="utf-8"))
        data["storage"]["root"] = str(root / "runs")
        data["agent"]["max_steps"] = initial
        if single:
            data["agent"]["plugin"] = "single_call_agent"
        data["evolution"]["mode"] = "llm"
        data["routing"] = {"plugin": "llm_router", "fixed_target": None, "manual_decisions": []}
        data["targets"] = {"allowed": ["prompt", "execution_limits"],
            "default_operators": {"prompt": "simple_prompt_evolver", "execution_limits": "step_limit_evolver"},
            "operator_configs": {"execution_limits": {"step_increment": 1, "max_steps": cap}}}
        data["limits"]["max_evolution_rounds"] = rounds
        path = root / "config.yaml"
        path.write_text(yaml.safe_dump(data), encoding="utf-8")
        return path

    def attach(self, task, choices=("execution_limits",), *, wrong=False, local_failure=False, fail_evolver=False,
               fail_validation=False):
        scripted = ScriptedModel(MODEL)
        decisions = iter(choices)
        actual = []
        def generate(request, ctx):
            body = json.loads(request.messages[-1].content)
            if body.get("operation") == "evolution_routing":
                target = next(decisions)
                result = response(json.dumps({"target": target, "rationale": "根据公开执行证据选择目标"}))
            elif body.get("operation") == "prompt_evolution":
                if fail_evolver:
                    from aer_core.errors import EvolutionError
                    from plugins.agents.execution import SessionRecord
                    record = SessionRecord()
                    record.add_usage(response("{}").usage)
                    actual.append(response("{}").usage)
                    raise EvolutionError("fixture evolution failure", stage="evolution", usage=record.usage())
                result = response('{"candidates":[{"prompt":"IMPROVED reusable instructions"}]}')
            elif fail_validation and ctx.stage == "similar_validation" and "/baseline/" not in ctx.operation_id:
                from aer_core.errors import ExecutionError
                from plugins.agents.execution import SessionRecord
                record = SessionRecord()
                record.add_usage(response("{}").usage)
                actual.append(response("{}").usage)
                raise ExecutionError("fixture validation failure", stage=ctx.stage, usage=record.usage())
            elif wrong or local_failure and body.get("public_metadata", {}).get("split") == "dev":
                result = response('{"answer":-999}')
            else:
                result = scripted.generate(request, ctx)
            actual.append(result.usage)
            return result
        model = RecordingModel(generate)
        task.execution.agent.model = model
        task.evolution.router.model = model
        task.evolution.resolver.resolve("prompt").operator.model = model
        return model, actual

    def round(self, root, number=1):
        path = next(root.glob(f"rounds/*/round-{number:03d}/record.json"))
        return json.loads(path.read_text(encoding="utf-8"))

    def assert_cost(self, result, actual):
        from plugins.agents.execution import SessionRecord
        record = SessionRecord()
        for item in actual:
            record.add_usage(item)
        self.assertEqual(result.usage.total_tokens, record.usage().total_tokens)
        self.assertEqual(result.usage.model_calls, record.usage().model_calls)

    def test_step_candidate_is_paired_committed_and_reused_with_full_router_log(self):
        with tempfile.TemporaryDirectory() as directory:
            startup = prepare_orchestrator(self.config(Path(directory)), run_id="router-test")
            task = startup.prepare_task("dev-001")
            initial = task.execution.storage.version_store.stable()
            model, actual = self.attach(task)
            result = task.run()
            self.assertEqual(result.stop_reason, "task_succeeded")
            stable = task.execution.storage.version_store.stable()
            self.assertEqual(stable.artifacts["prompt"], initial.artifacts["prompt"])
            self.assertEqual(ExecutionLimits.model_validate_json(task.execution.storage.artifact_store.get(stable.artifacts["execution_limits"])).max_steps, 2)
            self.assertEqual(result.final_execution.state_summary["effective_max_steps"], 2)
            self.assert_cost(result, actual)
            root = startup.execution.storage.run_root
            data = self.round(root)
            self.assertTrue(data["candidates"][0]["committed"])
            pairs = data["candidates"][0]["validation"]["tasks"]
            self.assertTrue(all(pair["baseline"]["execution"]["state_summary"]["effective_max_steps"] == 1 for pair in pairs))
            self.assertTrue(all(pair["candidate"]["execution"]["state_summary"]["effective_max_steps"] == 2 for pair in pairs))
            call = next(request for request, ctx in model.requests if ctx.stage == "routing")
            self.assertEqual(data["routing"]["model_call"]["messages"][0]["content"], call.messages[0].content)
            self.assertEqual(data["routing"]["model_call"]["messages"][1]["content"], json.loads(call.messages[1].content))
            text = (root / "evolution.txt").read_text(encoding="utf-8")
            self.assertIn(call.messages[0].content, text)
            self.assertIn(call.messages[1].content, text)
            self.assertNotIn("报告异常", text)
            self.assertIn("maximum model-call steps", call.messages[1].content)
            attempts = [event.payload for event in task.execution.storage.experience_store.events() if event.record_type == "attempt_finished"]
            self.assertEqual(attempts[0].usage.model_calls, 1)
            self.assertEqual(data["generation"]["evolver_usage"]["model_calls"], 0)
            output = Path(directory) / "run-result.json"
            output.write_text(json.dumps(startup.save_record(result)), encoding="utf-8")
            (root / "evolution.txt").unlink()
            with patch.object(model, "generate", side_effect=AssertionError("offline export must not call a model")):
                export_saved_run(output)
            self.assertIn(call.messages[1].content, (root / "evolution.txt").read_text(encoding="utf-8"))

    def test_rejected_and_empty_step_candidates_preserve_stable_version(self):
        for cap in (1, 3):
            with self.subTest(cap=cap), tempfile.TemporaryDirectory() as directory:
                startup = prepare_orchestrator(self.config(Path(directory), cap=cap), run_id="router-test")
                task = startup.prepare_task("dev-001")
                initial = task.execution.storage.version_store.stable()
                _, actual = self.attach(task, wrong=True)
                result = task.run()
                self.assertEqual(task.execution.storage.version_store.stable(), initial)
                self.assertEqual(result.stop_reason, "max_evolution_rounds_reached")
                data = self.round(startup.execution.storage.run_root)
                self.assertEqual(data["candidates"][0]["status"] if cap > 1 else data["status"], "rejected" if cap > 1 else "empty_candidates")
                self.assert_cost(result, actual)

    def test_invalid_route_logs_actual_prompt_and_ends_without_calling_an_evolver(self):
        with tempfile.TemporaryDirectory() as directory:
            startup = prepare_orchestrator(self.config(Path(directory)), run_id="router-test")
            task = startup.prepare_task("dev-001")
            initial = task.execution.storage.version_store.stable()
            model, actual = self.attach(task, choices=("memory",))
            result = task.run()
            self.assertEqual(result.stop_reason, "routing_error")
            self.assertEqual(result.usage.model_calls, 2)
            self.assertEqual(task.execution.storage.version_store.stable(), initial)
            data = self.round(startup.execution.storage.run_root)
            self.assertIsNone(data["routing"]["decision"])
            self.assertIsNotNone(data["routing"]["model_call"]["error"])
            self.assertTrue(data["routing"]["model_call"]["messages"])
            self.assertEqual(data["generation"]["evolver_usage"]["model_calls"], 0)
            self.assertFalse(data["candidates"])
            self.assert_cost(result, actual)

    def test_prompt_failure_counts_router_and_evolver_costs_once(self):
        with tempfile.TemporaryDirectory() as directory:
            startup = prepare_orchestrator(self.config(Path(directory)), run_id="router-test")
            task = startup.prepare_task("dev-001")
            _, actual = self.attach(task, choices=("prompt",), fail_evolver=True)
            result = task.run()
            self.assertEqual(result.stop_reason, "evolution_error")
            self.assertEqual(result.usage.model_calls, 3)
            self.assert_cost(result, actual)

    def test_step_candidate_with_incomplete_validation_is_not_committed(self):
        with tempfile.TemporaryDirectory() as directory:
            startup = prepare_orchestrator(self.config(Path(directory)), run_id="router-test")
            task = startup.prepare_task("dev-001")
            initial = task.execution.storage.version_store.stable()
            _, actual = self.attach(task, fail_validation=True)
            result = task.run()
            self.assertEqual(result.stop_reason, "validation_error")
            self.assertEqual(task.execution.storage.version_store.stable(), initial)
            data = self.round(startup.execution.storage.run_root)
            self.assertEqual(data["candidates"][0]["status"], "incomplete")
            self.assertFalse(data["candidates"][0]["committed"])
            self.assert_cost(result, actual)

    def test_second_round_uses_committed_limits_when_router_switches_to_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            startup = prepare_orchestrator(self.config(Path(directory), rounds=2), run_id="router-test")
            task = startup.prepare_task("dev-001")
            _, actual = self.attach(task, choices=("execution_limits", "prompt"), local_failure=True)
            result = task.run()
            root = startup.execution.storage.run_root
            first, second = self.round(root, 1), self.round(root, 2)
            self.assertTrue(first["candidates"][0]["committed"])
            self.assertEqual(second["routing"]["decision"]["target"], "prompt")
            self.assertEqual(second["input_to_evolver"]["stable_components"]["execution_limits"], {"max_steps": 2})
            self.assertEqual(set(second["candidates"][0]["components"]), {"prompt"})
            self.assertEqual(result.final_execution.state_summary["effective_max_steps"], 2)
            self.assert_cost(result, actual)

    def test_configuration_rejects_incompatible_agents_and_invalid_operator_settings(self):
        for single, changes in [(True, {}), (False, {"step_increment": 0}), (False, {"max_steps": True})]:
            with self.subTest(single=single, changes=changes), tempfile.TemporaryDirectory() as directory:
                path = self.config(Path(directory), single=single)
                data = yaml.safe_load(path.read_text(encoding="utf-8"))
                data["targets"]["operator_configs"]["execution_limits"].update(changes)
                path.write_text(yaml.safe_dump(data), encoding="utf-8")
                with self.assertRaises(ConfigError):
                    load_experiment_config(path)
