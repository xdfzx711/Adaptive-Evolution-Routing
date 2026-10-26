import json
from pathlib import Path
import tempfile
import unittest

import yaml

from aer_core.errors import ConfigError
from experiments.configuration import load_experiment_config
from experiments.orchestration import prepare_orchestrator
from plugins.tool_selection import ToolSelection
from plugins.providers.scripted import ScriptedModel
from tests.execution.helpers import MODEL, RecordingModel, response
from tests.integration.test_codex_routing import config
from tests.tools.helpers import LOCAL_CATALOG


def tool_config(root, mode="tools"):
    path = config(root, mode=mode)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["agent"]["config"].update(tooling={"catalog": LOCAL_CATALOG}, initial_tools=[])
    data["targets"]["allowed"] = ["prompt", "model", "tool"]
    data["targets"]["default_operators"]["tool"] = "tool_selection_evolver"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def attach_tool(task, selected=None):
    scripted = ScriptedModel(MODEL)
    def generate(request, ctx):
        body = json.loads(request.messages[-1].content)
        if body.get("operation") == "evolution_routing":
            return response('{"target":"tool","rationale":"Add a structured analysis tool"}')
        if body.get("operation") == "tool_selection":
            return response(json.dumps({"enabled_tools": ["python_syntax_check"] if selected is None else selected,
                "rationale": "Check source syntax"}))
        return scripted.generate(request, ctx)
    model = RecordingModel(generate)
    task.execution.diagnoser.model = model
    task.evolution.router.model = model
    task.evolution.resolver.resolve("tool").operator.model = model
    return model


class ToolRoutingTests(unittest.TestCase):
    def test_legacy_assembly_does_not_make_an_unconfigured_tool_target_feasible(self):
        with tempfile.TemporaryDirectory() as directory:
            path = tool_config(Path(directory))
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            data["agent"]["config"].pop("tooling")
            data["agent"]["config"].pop("initial_tools")
            path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
            startup = prepare_orchestrator(path, run_id="legacy-tool-test", console=False)
            task = startup.prepare_task("dev-001")
            stable = task.execution.storage.version_store.stable()
            self.assertNotIn("tool", stable.artifacts)
            availability = task.evolution.targets.feasible(task.execution.agent, stable, ["prompt", "model", "tool"])
            self.assertNotIn("tool", availability.targets)
            tool = next(item for item in availability.availability if item.target == "tool")
            self.assertEqual(tool.reason_code, "target_artifact_missing")

    def test_tool_candidate_is_assembled_paired_validated_committed_and_logged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            startup = prepare_orchestrator(tool_config(root), run_id="tool-test", console=False)
            task = startup.prepare_task("dev-001")
            stable = task.execution.storage.version_store.stable()
            model = attach_tool(task)
            self.assertIn("tool", task.evolution.feasible_targets(stable))
            result = task.run()
            self.assertEqual(result.stop_reason, "task_succeeded", result.error)
            committed = task.execution.storage.version_store.stable()
            self.assertEqual(committed.artifacts["prompt"], stable.artifacts["prompt"])
            self.assertEqual(committed.artifacts["model"], stable.artifacts["model"])
            self.assertEqual(committed.config, stable.config)
            self.assertEqual(ToolSelection.model_validate_json(task.execution.storage.artifact_store.get(committed.artifacts["tool"])).enabled_tools,
                ("python_syntax_check",))
            state = result.final_execution.state_summary
            self.assertEqual(state["selected_tools"], ("python_syntax_check",))
            self.assertEqual(state["available_tools"], ("python_syntax_check",))
            self.assertEqual(state["tool_setup_status"], "ready")
            self.assertEqual(state["tool_invocations"][0]["status"], "completed")
            record = json.loads(next(startup.execution.storage.run_root.glob("rounds/*/round-001/record.json")).read_text(encoding="utf-8"))
            candidate = record["candidates"][0]
            self.assertTrue(candidate["committed"])
            pairs = candidate["validation"]["tasks"]
            self.assertTrue(all(pair["baseline"]["execution"]["state_summary"]["selected_tools"] == [] for pair in pairs))
            self.assertTrue(all(pair["candidate"]["execution"]["state_summary"]["available_tools"] == ["python_syntax_check"] for pair in pairs))
            self.assertTrue(all(not pair["candidate_cached"] for pair in pairs))
            prompt = next(json.loads(request.messages[-1].content) for request, _ in model.requests
                if json.loads(request.messages[-1].content).get("operation") == "tool_selection")
            self.assertEqual(prompt["current_selection"], {"enabled_tools": []})
            self.assertNotIn("tooling", prompt["context"])
            self.assertEqual(len(list((root / "workspaces").iterdir())), len(pairs) * 2)
            text = (startup.execution.storage.run_root / "evolution.txt").read_text(encoding="utf-8")
            self.assertIn("附加工具：python_syntax_check", text)
            self.assertIn("工具装配：ready", text)

    def test_rejected_and_interrupted_validation_preserve_the_stable_tool_set(self):
        for mode, stop in (("tools-reject", "max_evolution_rounds_reached"), ("tools-validation-error", "validation_error")):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                startup = prepare_orchestrator(tool_config(Path(directory), mode=mode), run_id="tool-test", console=False)
                task = startup.prepare_task("dev-001")
                stable = task.execution.storage.version_store.stable()
                attach_tool(task)
                result = task.run()
                self.assertEqual(result.stop_reason, stop)
                self.assertEqual(task.execution.storage.version_store.stable(), stable)

    def test_new_configuration_fields_are_validated_before_assembly(self):
        with tempfile.TemporaryDirectory() as directory:
            path = tool_config(Path(directory))
            for change in ("agent.config.initial_tools=[missing]", "agent.config.tooling.tool_timeout_seconds=-1",
                           "agent.config.tooling.catalog.python_structure.plugin=missing"):
                with self.subTest(change=change), self.assertRaises(ConfigError):
                    load_experiment_config(path, overrides=[change])
