import json
from pathlib import Path
import tempfile
import unittest

import yaml

from experiments.orchestration import prepare_orchestrator
from plugins.memory.library import decode_bank, encode_bank
from tests.integration.test_codex_routing import config, ROOT
from tests.evolution.test_reasoningbank import FakeEmbedding, ITEM, memory_config, entry
from tests.execution.helpers import RecordingModel, response


def baseline_config(root, mode="memory-evolution", task_mode="independent"):
    path = config(root, mode=mode)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["assessment"]["diagnoser"] = "lightweight_diagnoser"
    data["agent"]["config"]["memory"] = memory_config().model_dump(mode="json")
    data["agent"]["config"]["skills"] = None
    data["state"]["task_mode"] = task_mode
    data["evolution"]["mode"] = "fixed"
    data["routing"] = {"plugin": "fixed_router", "fixed_target": "memory", "manual_decisions": []}
    data["targets"] = {"allowed": ["memory"], "default_operators": {"memory": "reasoningbank_memory_evolver"}}
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


class FixedMemoryIntegrationTests(unittest.TestCase):
    def test_success_extraction_and_failed_embedding_cost(self):
        from aer_core.errors import ExecutionError
        from aer_core.schemas.usage import ResourceUsage
        from tests.execution.helpers import context
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = baseline_config(root)
            library = root / "initial.jsonl"
            library.write_bytes(encode_bank([entry()]))
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            data["agent"]["config"]["memory"]["initial_library"] = str(library)
            data["evolution"]["stop_on_success"] = False
            path.write_text(yaml.safe_dump(data), encoding="utf-8")
            startup = prepare_orchestrator(path, run_id="memory-success")
            task = startup.prepare_task("dev-001")
            task.execution.agent.memory.embedder = FakeEmbedding()
            model = RecordingModel(lambda *_: response(ITEM))
            task.evolution.resolver.resolve("memory").operator.model = model
            result = task.run()
            self.assertTrue(result.final_evaluation.success)
            self.assertEqual(result.evolution_rounds_used, 1)
            self.assertTrue(json.loads(model.requests[0][0].messages[1].content)["evaluation_signal"]["success"])
            self.assertEqual(len(decode_bank(task.execution.storage.artifact_store.get(
                task.execution.storage.version_store.stable().artifacts["memory"]))), 1)

            class FailedEmbedding:
                def embed(self, text, ctx):
                    raise ExecutionError("fixture failure", stage=ctx.stage, operation_id=ctx.operation_id,
                        usage=ResourceUsage(input_tokens=7, output_tokens=0, total_tokens=7, model_calls=1,
                            tool_calls=0, elapsed_seconds=0, usage_complete=True, usage_sources=["simulated"]))
            agent = task.execution.agent
            agent.memory.vectors.clear()
            agent.memory.embedder = FailedEmbedding()
            public = task.execution.benchmark.benchmark.task_view(task.task)
            failed = agent.run(public, task.execution.storage.version_store.stable(), context())
            self.assertEqual(failed.status, "error")
            self.assertEqual(failed.usage.total_tokens, 7)
            self.assertFalse(failed.state_summary["session_started"])

    def test_fixed_loop_acceptance_rejection_records_and_episode_scope(self):
        for mode, commits in (("memory-evolution", True), ("memory-evolution-reject", False)):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                startup = prepare_orchestrator(baseline_config(root, mode), run_id="memory-test")
                task = startup.prepare_task("dev-001")
                task.execution.agent.memory.embedder = FakeEmbedding()
                model = RecordingModel(lambda *_: response(ITEM))
                task.evolution.resolver.resolve("memory").operator.model = model
                initial = task.execution.storage.version_store.stable()
                result = task.run()
                self.assertEqual(result.stop_reason, "task_succeeded" if commits else "max_evolution_rounds_reached")
                stable = task.execution.storage.version_store.stable()
                self.assertEqual(stable != initial, commits)
                self.assertEqual(stable.artifacts["prompt"], initial.artifacts["prompt"])
                bank = decode_bank(task.execution.storage.artifact_store.get(stable.artifacts["memory"]))
                self.assertEqual(len(bank), int(commits))
                self.assertTrue(stable.artifacts["memory"].uri.endswith(".jsonl"))
                report = json.loads(next(startup.execution.storage.run_root.glob("rounds/*/round-001/record.json")).read_text(encoding="utf-8"))
                self.assertEqual(report["candidates"][0]["committed"], commits)
                self.assertEqual(report["generation"]["memory_evolution"]["input"]["evaluation_signal"]["evaluator_id"], "deterministic_evaluator")
                self.assertEqual(len(model.requests), 1)
                for pair in report["candidates"][0]["validation"]["tasks"]:
                    self.assertEqual(pair["baseline"]["execution"]["state_summary"]["memory_experience_count"], 0)
                    self.assertEqual(pair["candidate"]["execution"]["state_summary"]["memory_experience_count"], 1)
                other = startup.prepare_task("dev-002")
                self.assertEqual(other.execution.storage.version_store.stable().artifacts["memory"], initial.artifacts["memory"])
                self.assertIn("ReasoningBank", (startup.execution.storage.run_root / "evolution.txt").read_text(encoding="utf-8"))

    def test_continual_scope_and_reopening_use_committed_bank(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = baseline_config(root, task_mode="continual")
            startup = prepare_orchestrator(path, run_id="memory-continual")
            first = startup.prepare_task("dev-001")
            first.execution.agent.memory.embedder = FakeEmbedding()
            first.evolution.resolver.resolve("memory").operator.model = RecordingModel(lambda *_: response(ITEM))
            first.run()
            committed = first.execution.storage.version_store.stable()
            reopened = prepare_orchestrator(path, run_id="memory-continual")
            second = reopened.prepare_task("dev-003")
            self.assertEqual(second.execution.storage.version_store.stable(), committed)
            second.execution.agent.memory.embedder = FakeEmbedding()
            result = second.run()
            self.assertEqual(result.stop_reason, "task_succeeded")
            self.assertEqual(result.evolution_rounds_used, 0)
            self.assertEqual(result.final_execution.state_summary["memory_experience_count"], 1)
