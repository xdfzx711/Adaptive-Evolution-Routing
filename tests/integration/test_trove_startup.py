import json
from pathlib import Path
import tempfile
import unittest

import yaml

from experiments.configuration import load_experiment_config
from experiments.orchestration import prepare_orchestrator
from plugins.evolution.trove.library import read_library
from plugins.providers.scripted import ScriptedModel
from tests.evolution.test_trove import proposer
from tests.execution.helpers import MODEL, RecordingModel, response
from tests.integration.test_codex_routing import config


def trove_config(root, mode="tools"):
    path = config(root, mode=mode)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["agent"]["config"].update(tooling={"catalog": {}, "generated_python": True}, initial_tools=[])
    data["targets"]["allowed"] = ["prompt", "model", "tool"]
    data["targets"]["default_operators"]["tool"] = "trove_tool_evolver"
    data["targets"]["operator_configs"] = {"tool": {"samples_per_mode": 1}}
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def attach(task):
    scripted = ScriptedModel(MODEL)
    def generate(query, ctx):
        body = json.loads(query.messages[-1].content)
        if body.get("operation", "").startswith("trove_"):
            return proposer(query, ctx)
        if body.get("operation") == "evolution_routing":
            return response('{"target":"tool","rationale":"Improve reusable capabilities"}')
        return scripted.generate(query, ctx)
    model = RecordingModel(generate)
    task.execution.diagnoser.model = model
    task.evolution.router.model = model
    task.evolution.resolver.resolve("tool").operator.model = model
    return model


class TroveStartupTests(unittest.TestCase):
    def test_toolbox_passes_existing_route_validation_commit_and_restore(self):
        with tempfile.TemporaryDirectory() as directory:
            path = trove_config(Path(directory))
            loaded = load_experiment_config(path)
            self.assertEqual(loaded.config.targets.default_operators["tool"], "trove_tool_evolver")
            startup = prepare_orchestrator(path, run_id="trove-test", console=False)
            task = startup.prepare_task("dev-001")
            stable = task.execution.storage.version_store.stable()
            attach(task)
            self.assertIn("tool", task.evolution.feasible_targets(stable))
            result = task.run()
            self.assertEqual(result.stop_reason, "task_succeeded", result.error)
            committed = task.execution.storage.version_store.stable()
            self.assertNotEqual(committed.artifacts["tool"], stable.artifacts["tool"])
            self.assertEqual(committed.artifacts["prompt"], stable.artifacts["prompt"])
            self.assertEqual(committed.artifacts["model"], stable.artifacts["model"])
            self.assertEqual(committed.config, stable.config)
            library = read_library(task.execution.storage.artifact_store.get(committed.artifacts["tool"]))
            self.assertEqual(library.enabled_tools, ("path_info",))
            self.assertEqual(result.final_execution.state_summary["available_tools"], ("path_info",))
            self.assertEqual(result.final_execution.state_summary["tool_invocations"][0]["status"], "completed")
            task.execution.agent.restore(stable)
            self.assertFalse(task.execution.agent.tool_adapter.read(stable, task.execution.storage.artifact_store).enabled_tools)
            task.execution.agent.restore(committed)
            self.assertEqual(task.execution.agent.tool_adapter.read(committed, task.execution.storage.artifact_store), library)
            record = json.loads(next(startup.execution.storage.run_root.glob("rounds/*/round-001/record.json")).read_text(encoding="utf-8"))
            candidate = record["candidates"][0]
            self.assertTrue(candidate["committed"])
            self.assertEqual(set(candidate["components"]), {"tool"})
            pairs = candidate["validation"]["tasks"]
            self.assertTrue(all(pair["baseline"]["execution"]["state_summary"]["available_tools"] == [] for pair in pairs))
            self.assertTrue(all(pair["candidate"]["execution"]["state_summary"]["available_tools"] == ["path_info"] for pair in pairs))
            self.assertTrue(all(not pair["candidate_cached"] for pair in pairs))

    def test_rejected_or_interrupted_validation_cannot_change_stable_toolbox(self):
        for mode, stop in (("tools-reject", "max_evolution_rounds_reached"), ("tools-validation-error", "validation_error")):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                startup = prepare_orchestrator(trove_config(Path(directory), mode), run_id="trove-test", console=False)
                task = startup.prepare_task("dev-001")
                stable = task.execution.storage.version_store.stable()
                attach(task)
                result = task.run()
                self.assertEqual(result.stop_reason, stop, result.error)
                self.assertEqual(task.execution.storage.version_store.stable(), stable)
