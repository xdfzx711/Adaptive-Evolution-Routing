import json
import sys
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from aer_core.errors import ConfigError
from experiments.configuration import load_experiment_config
from experiments.orchestration import prepare_orchestrator
from experiments.report_export import export_saved_run
from plugins.agents.model_selection import ModelSelection
from plugins.providers.scripted import ScriptedModel
from tests.execution.helpers import MODEL, RecordingModel, response
from tests.execution.test_codex import FAKE


ROOT = Path(__file__).resolve().parents[2]


def config(root, mode="model", rounds=1):
    data = yaml.safe_load((ROOT / "experiments/configs/v0_1_codex_router.yaml").read_text(encoding="utf-8"))
    data["storage"]["root"] = str(root / "runs")
    data["benchmark"]["plugin"] = "toy_tasks"
    data["providers"]["model"] = MODEL.model_dump(mode="json")
    data["agent"]["config"].update(command=[sys.executable, str(FAKE), "--fixture-mode", mode], workspace_root=str(root / "workspaces"))
    data["limits"]["max_evolution_rounds"] = rounds
    path = root / "config.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def attach(task, choices=("model",), selected="gpt-6-sol"):
    decisions = iter(choices)
    scripted = ScriptedModel(MODEL)
    def generate(request, ctx):
        body = json.loads(request.messages[-1].content)
        operation = body.get("operation")
        if operation == "evolution_routing":
            return response(json.dumps({"target": next(decisions), "rationale": "根据公开轨迹选择组件"}))
        if operation == "model_selection":
            return response(json.dumps({"model_id": selected, "rationale": "验证另一执行模型"}))
        if operation == "prompt_evolution":
            return response(json.dumps({"candidates": [{"prompt": "IMPROVED reusable instructions", "rationale": "format and strategy"}]}))
        return scripted.generate(request, ctx)
    model = RecordingModel(generate)
    task.execution.diagnoser.model = model
    task.evolution.router.model = model
    for name in ("prompt", "model"):
        task.evolution.resolver.resolve(name).operator.model = model
    return model


class CodexRoutingIntegrationTests(unittest.TestCase):
    def test_cli_overrides_are_used_by_the_entire_run_without_editing_yaml(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = config(root)
            original = path.read_bytes()
            output = root / "cli-result.json"
            completed = subprocess.run([sys.executable, "main.py", "run-task", str(path), "--task-id", "dev-001",
                "--set", "agent.config.initial_model=gpt-6-luna", "--quiet", "--output", str(output)],
                cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=30)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads(output.read_text(encoding="utf-8"))["record"]
            self.assertEqual(result["final_execution"]["state_summary"]["execution_model"], "gpt-6-luna")
            self.assertEqual(result["evolution_rounds_used"], 0)
            self.assertEqual(path.read_bytes(), original)

    def test_model_candidate_commits_with_full_router_call_only_in_round_record(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            startup = prepare_orchestrator(config(root), run_id="codex-test", console=False)
            task = startup.prepare_task("dev-001")
            initial = task.execution.storage.version_store.stable()
            model = attach(task)
            result = task.run()
            self.assertEqual(result.stop_reason, "task_succeeded")
            stable = task.execution.storage.version_store.stable()
            self.assertEqual(stable.artifacts["prompt"], initial.artifacts["prompt"])
            self.assertEqual(ModelSelection.model_validate_json(task.execution.storage.artifact_store.get(stable.artifacts["model"])).model_id, "gpt-6-sol")
            self.assertEqual(result.final_execution.state_summary["execution_model"], "gpt-6-sol")
            self.assertIsNone(result.usage.model_calls)
            self.assertTrue(result.usage.usage_complete)
            run_root = startup.execution.storage.run_root
            record = json.loads(next(run_root.glob("rounds/*/round-001/record.json")).read_text(encoding="utf-8"))
            self.assertTrue(record["candidates"][0]["committed"])
            pairs = record["candidates"][0]["validation"]["tasks"]
            self.assertTrue(all(pair["baseline"]["execution"]["state_summary"]["execution_model"] == "gpt-5.6-sol" for pair in pairs))
            self.assertTrue(all(pair["candidate"]["execution"]["state_summary"]["execution_model"] == "gpt-6-sol" for pair in pairs))
            routing = next(request for request, ctx in model.requests if ctx.stage == "routing")
            self.assertEqual(record["routing"]["model_call"]["messages"][0]["content"], routing.messages[0].content)
            self.assertEqual(record["routing"]["model_call"]["messages"][1]["content"], json.loads(routing.messages[1].content))
            text = (run_root / "evolution.txt").read_text(encoding="utf-8")
            self.assertNotIn(routing.messages[1].content, text)
            self.assertNotIn('"trajectory"', text)
            self.assertNotIn("最终解析输出：", text)
            self.assertNotIn("最终输出：", text)
            self.assertIn("路由理由：根据公开轨迹选择组件", text)
            self.assertIn(" / routing.model_call", text)
            # Exactly one workspace per actual observation; commit does not retest.
            self.assertEqual(len(list((root / "workspaces").iterdir())), len(pairs) * 2)
            output = root / "result.json"
            output.write_text(json.dumps(startup.save_record(result)), encoding="utf-8")
            (run_root / "evolution.txt").unlink()
            with patch.object(model, "generate", side_effect=AssertionError("offline export cannot invoke models")):
                export_saved_run(output)
            rebuilt = (run_root / "evolution.txt").read_text(encoding="utf-8")
            self.assertNotIn(routing.messages[1].content, rebuilt)
            self.assertNotIn('"trajectory"', rebuilt)
            self.assertIn("路由理由：根据公开轨迹选择组件", rebuilt)

    def test_rejection_empty_selection_and_validation_error_preserve_the_stable_version(self):
        for mode, selected, stop in (("reject", "gpt-6-sol", "max_evolution_rounds_reached"),
            ("model", "gpt-5.6-sol", "max_evolution_rounds_reached"),
            ("validation-error", "gpt-6-sol", "validation_error")):
            with self.subTest(mode=mode, selected=selected), tempfile.TemporaryDirectory() as directory:
                startup = prepare_orchestrator(config(Path(directory), mode), run_id="codex-test", console=False)
                task = startup.prepare_task("dev-001")
                initial = task.execution.storage.version_store.stable()
                attach(task, selected=selected)
                result = task.run()
                self.assertEqual(result.stop_reason, stop)
                self.assertEqual(task.execution.storage.version_store.stable(), initial)

    def test_next_prompt_round_inherits_the_committed_model(self):
        with tempfile.TemporaryDirectory() as directory:
            startup = prepare_orchestrator(config(Path(directory), "local-failure", rounds=2), run_id="codex-test", console=False)
            task = startup.prepare_task("dev-001")
            attach(task, ("model", "prompt"))
            result = task.run()
            self.assertEqual(result.evolution_rounds_used, 2)
            record = json.loads(next(startup.execution.storage.run_root.glob("rounds/*/round-002/record.json")).read_text(encoding="utf-8"))
            self.assertEqual(record["input_to_evolver"]["stable_components"]["model"]["model_id"], "gpt-6-sol")
            self.assertEqual(result.final_execution.state_summary["execution_model"], "gpt-6-sol")

    def test_invalid_route_and_invalid_model_config_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = config(root)
            startup = prepare_orchestrator(path, run_id="codex-test", console=False)
            task = startup.prepare_task("dev-001")
            attach(task, ("memory",))
            self.assertEqual(task.run().stop_reason, "routing_error")
            for change in ("agent.config.initial_model=unknown", "agent.max_steps=8", "agent.config.command=[]"):
                with self.subTest(change=change), self.assertRaises(ConfigError):
                    load_experiment_config(path, overrides=[change])

