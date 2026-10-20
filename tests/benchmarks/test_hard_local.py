import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path

import yaml

from aer_core.errors import BenchmarkError
from aer_core.schemas.experience import VersionCommittedEvent
from experiments.benchmark import prepare_benchmark
from experiments.orchestration import prepare_orchestrator
from plugins.benchmarks.hard_local import HardLocalBenchmark
from plugins.benchmarks.toy_tasks import ToyBenchmark
from plugins.evaluators.deterministic import DeterministicEvaluator
from plugins.providers.scripted import ScriptedModel
from tests.benchmarks.hard_reference import SOLVERS
from tests.domain_helpers import execution
from tests.execution.helpers import MODEL, RecordingModel, response


ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "plugins/benchmarks/hard_local"


class HardLocalTests(unittest.TestCase):
    def setUp(self):
        self.benchmark = HardLocalBenchmark()
        self.evaluator = DeterministicEvaluator()

    def expected(self, task):
        params = self.benchmark.evaluation_spec(task).model_dump(mode="json")["private_parameters"]
        return {"answer": params["expected"]} if "absolute_tolerance" in params else params["expected"]

    def score(self, task, output):
        return self.evaluator.evaluate_record(task, execution().replace(task_id=task.task_id, output=output),
            self.benchmark.evaluation_spec(task), operation_id="hard-evaluation")

    def test_partition_sizes_families_and_public_private_separation(self):
        self.assertEqual([len(self.benchmark.tasks(split)) for split in ("dev", "validation", "test")], [6, 10, 6])
        groups = Counter(task.metadata["validation_group"] for task in self.benchmark.tasks("validation"))
        self.assertEqual(groups, {"similar": 6, "regression": 4})
        self.assertEqual({task.metadata["family"] for task in self.benchmark.tasks("dev")}, set(SOLVERS))
        self.assertEqual({task.metadata["family"] for task in self.benchmark.tasks("test")}, set(SOLVERS))
        tasks = [task for split in ("dev", "validation", "test") for task in self.benchmark.tasks(split)]
        self.assertEqual(len({task.task_id for task in tasks}), 22)
        self.assertEqual(len({task.metadata["source_id"] for task in tasks}), 22)
        inputs = [json.dumps(task.model_dump(mode="json")["input"], sort_keys=True) for task in tasks]
        self.assertEqual(len(set(inputs)), 22)
        for task in tasks:
            public = self.benchmark.task_view(task).model_dump_json()
            self.assertNotIn('"expected"', public)
            self.assertNotIn("eval_spec_ref", public)
            self.assertNotIn("private_parameters", public)
            self.assertNotIn("scoring_specs", public)
        self.assertEqual([len(ToyBenchmark().tasks(split)) for split in ("dev", "validation", "test")], [4, 8, 4])

    def test_authored_development_answers_have_hand_checked_anchors(self):
        answers = [
            dict(subtotal_cents=13485, discount_cents=1685, shipping_cents=499, tax_cents=974, total_cents=13273),
            dict(allocations="A=80;B=100;C=91;D=0;E=46", unallocated=0),
            dict(ids="B,I,F,G", count=4, score_total=372),
            dict(start_minute=675, end_minute=720, possible=True),
            dict(tags="Z|ALPHA-BETA|A", counts="3,3,2", unique_count=5),
            dict(ids="C,E,F,G", cost=22, value=60, possible=True)]
        for task, answer in zip(self.benchmark.tasks("dev"), answers):
            with self.subTest(task=task.task_id):
                self.assertEqual(self.expected(task), answer)
                self.assertTrue(self.score(task, answer).success)

    def test_all_labels_match_exact_test_side_rules_and_score_reproducibly(self):
        for split in ("dev", "validation", "test"):
            for task in self.benchmark.tasks(split):
                family = task.metadata["family"]
                output = self.expected(task)
                if family in SOLVERS:
                    self.assertEqual(SOLVERS[family](task.model_dump(mode="json")["input"]["data"]), output)
                self.assertTrue(self.score(task, output).success)
                self.assertEqual(self.score(task, output), self.score(task, output))
                incorrect = dict(output)
                name = next(iter(incorrect))
                incorrect[name] = incorrect[name] + 1 if type(incorrect[name]) is int else str(incorrect[name]) + "wrong"
                self.assertFalse(self.score(task, incorrect).success)
                self.assertEqual(self.score(task, {}).reason_code, "invalid_output_format")

    def test_threshold_tie_capacity_and_empty_results_have_fixed_answers(self):
        by_id = {task.task_id: task for split in ("validation", "test") for task in self.benchmark.tasks(split)}
        anchors = {
            "hard-similar-001": dict(subtotal_cents=11071, discount_cents=830, shipping_cents=650, tax_cents=1171, total_cents=12062),
            "hard-similar-002": dict(allocations="A=8;B=8;C=7;D=0;E=0", unallocated=0),
            "hard-similar-004": dict(start_minute=560, end_minute=600, possible=True),
            "hard-similar-006": dict(ids="B1,B2,R1", cost=14, value=36, possible=True),
            "hard-test-001": dict(subtotal_cents=4098, discount_cents=409, shipping_cents=311, tax_cents=50, total_cents=4050),
            "hard-test-002": dict(allocations="W=0;X=50;Y=125;Z=150", unallocated=175),
            "hard-test-003": dict(ids="", count=0, score_total=0),
            "hard-test-004": dict(start_minute=-1, end_minute=-1, possible=False),
            "hard-test-005": dict(tags="", counts="", unique_count=0),
        }
        for task_id, output in anchors.items():
            with self.subTest(task_id=task_id):
                self.assertEqual(self.expected(by_id[task_id]), output)

    def test_foreign_dataset_identity_and_foreign_spec_are_rejected(self):
        with self.assertRaises(BenchmarkError):
            ToyBenchmark(DATA / "tasks.json", DATA / "scoring_specs.json")
        task = self.benchmark.tasks("dev")[0]
        from aer_core.errors import EvaluationError
        spec = self.benchmark.evaluation_spec(task)
        with self.assertRaises(EvaluationError):
            self.evaluator.evaluate_record(task, execution().replace(task_id=task.task_id, output=self.expected(task)),
                spec.replace(task=spec.task.replace(benchmark_id="foreign")), operation_id="foreign-score")

    def local_config(self, root, mode="fixed"):
        source = ROOT / "experiments/configs/v0_1_qwen_hard_local.yaml"
        data = yaml.safe_load(source.read_text(encoding="utf-8"))
        data["storage"]["root"] = str(root / "runs")
        data["providers"]["model"] = MODEL.model_dump(mode="json")
        data["limits"]["max_evolution_rounds"] = 1
        if mode == "disabled":
            data["evolution"]["mode"] = mode
            data["limits"]["max_evolution_rounds"] = 0
            data["routing"] = dict(plugin=None, fixed_target=None, manual_decisions=[])
            data["targets"] = dict(allowed=[], default_operators={})
        path = root / "hard.yaml"
        path.write_text(yaml.safe_dump(data), encoding="utf-8")
        return path

    def test_config_loads_new_plugin_and_baseline_shares_initial_agent(self):
        from experiments.configuration import load_experiment_config
        fixed = load_experiment_config(ROOT / "experiments/configs/v0_1_qwen_hard_local.yaml").config
        baseline = load_experiment_config(ROOT / "experiments/configs/v0_1_qwen_hard_local_no_evolution.yaml").config
        self.assertEqual(fixed.agent, baseline.agent)
        self.assertEqual(fixed.providers, baseline.providers)
        self.assertEqual(fixed.benchmark, baseline.benchmark)
        self.assertEqual(baseline.evolution.mode, "disabled")
        with tempfile.TemporaryDirectory() as directory:
            prepared = prepare_benchmark(self.local_config(Path(directory)))
            self.assertEqual(prepared.benchmark.index().benchmark_id, "hard_local")
            self.assertEqual(len(prepared.similar_tasks), 6)
            self.assertEqual(len(prepared.regression_tasks), 4)
            self.assertEqual(prepared.as_dict()["execution_task_ids"][0], "hard-dev-001")

    def test_hard_task_flows_through_generation_validation_commit_and_result_reuse(self):
        with tempfile.TemporaryDirectory() as directory:
            startup = prepare_orchestrator(self.local_config(Path(directory)), run_id="hard")
            task = startup.prepare_task("hard-dev-001")
            scripted = ScriptedModel(MODEL)
            def reply(request, ctx):
                payload = json.loads(request.messages[-1].content)
                if payload.get("operation") == "failure_diagnosis":
                    return scripted.generate(request, ctx)
                if payload.get("operation") == "prompt_evolution":
                    return response('{"candidates":[{"prompt":"IMPROVED reusable multi-rule verification instructions."}]}')
                if not request.messages[0].content.startswith("IMPROVED"):
                    return response('{}')
                family = payload["public_metadata"]["family"]
                if family in SOLVERS:
                    return response(json.dumps(SOLVERS[family](payload["input"]["data"])))
                return scripted.generate(request.replace(tools=[]), ctx)
            model = RecordingModel(reply)
            task.execution.agent.model = model
            task.controller.diagnoser.model = model
            task.evolution.resolver.resolve("prompt").operator.model = model
            result = task.run()
            self.assertEqual(result.stop_reason, "task_succeeded")
            self.assertEqual(result.evolution_rounds_used, 1)
            self.assertNotEqual(result.initial_version_id, result.final_stable_version_id)
            self.assertEqual(result.usage.model_calls, len(model.requests))
            self.assertEqual(result.usage.model_calls, 24)
            self.assertEqual(len([event for event in task.execution.storage.experience_store.events()
                                  if isinstance(event, VersionCommittedEvent)]), 1)
            evolution_input = next(request.messages[-1].content for request, _ in model.requests
                                   if '"prompt_evolution"' in request.messages[-1].content)
            self.assertNotIn('"expected"', evolution_input)
            self.assertNotIn("hard-similar-", evolution_input)
            self.assertNotIn("hard-test-", evolution_input)
