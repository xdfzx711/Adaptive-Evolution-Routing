import ast
from contextlib import redirect_stdout
from hashlib import sha256
import io
import json
from pathlib import Path
import tempfile
import unittest

import yaml

from aer_core.errors import BenchmarkError, ContractError
from experiments.benchmark import prepare_benchmark
from plugins.benchmarks.hotpotqa import HotpotQABenchmark, HotpotQASettings
from plugins.benchmarks.manifest import TaskManifest, select_holdouts
from plugins.evaluators.hotpotqa import METRICS, score_prediction
from plugins.evaluators.hotpotqa_official.evaluate import eval as official_eval
from tests.benchmarks.hotpotqa_helpers import ROOT, config_file, fixture
from tests.domain_helpers import execution


class OfficialHotpotScoringTests(unittest.TestCase):
    def test_all_metrics_match_official_script_with_partial_and_missing_predictions(self):
        cases = [
            ({"answer": "The Eiffel Tower!", "supporting_facts": [["Paris", 0], ["Paris", 0]]}, "eiffel tower", [["Paris", 0]]),
            ({"answer": "red fox", "supporting_facts": [["Doc", 0], ["Wrong", 3]]}, "red fox animal", [["Doc", 0], ["Doc", 1]]),
            ({"answer": "yes", "supporting_facts": [["Doc", 0]]}, "no", [["Doc", 0]]),
            ({"answer": "noanswer", "supporting_facts": []}, "noanswer", [["Doc", 0]]),
            ({"answer": ""}, "something", [["Doc", 0]]),
            ({"supporting_facts": [["Doc", -1]]}, "a", [["Doc", 0]]),
            ({}, "x", [["Doc", 0]]),
        ]
        gold, predictions, observed = [], {"answer": {}, "sp": {}}, []
        for i, (output, answer, facts) in enumerate(cases):
            key = str(i)
            gold.append({"_id": key, "answer": answer, "supporting_facts": facts})
            if "answer" in output:
                predictions["answer"][key] = output["answer"]
            if "supporting_facts" in output:
                predictions["sp"][key] = output["supporting_facts"]
            observed.append(score_prediction(output, answer, facts)[0])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "gold.json").write_text(json.dumps(gold), encoding="utf-8")
            (root / "pred.json").write_text(json.dumps(predictions), encoding="utf-8")
            output = io.StringIO()
            with redirect_stdout(output):
                official_eval(root / "pred.json", root / "gold.json")
            expected = ast.literal_eval(output.getvalue().splitlines()[-1])
        for key in METRICS:
            self.assertAlmostEqual(sum(row[key] for row in observed) / len(cases), expected[key], msg=key)

    def test_vendor_is_original_except_json_import(self):
        root = ROOT / "plugins/evaluators/hotpotqa_official"
        provenance = json.loads((root / "PROVENANCE.json").read_text(encoding="utf-8"))
        restored = (root / "evaluate.py").read_bytes().replace(
            b'import json  # AER: standard-library JSON; scoring unchanged.', b'import ujson as json')
        self.assertEqual(sha256(restored).hexdigest(), provenance["original_sha256"])

    def test_malformed_output_is_scored_without_changing_valid_fields(self):
        metrics, valid = score_prediction({"answer": "yes", "supporting_facts": [["A", True]]}, "yes", [["A", 0]])
        self.assertFalse(valid)
        self.assertEqual(metrics["em"], 1)
        self.assertEqual(metrics["joint_f1"], 0)


class HotpotBenchmarkTests(unittest.TestCase):
    def test_per_task_holdouts_and_private_label_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, rows, manifest = config_file(root)
            prepared = prepare_benchmark(path)
            self.assertNotIn("PRIVATE_ANSWER", json.dumps(prepared.as_dict()))
            for task in prepared.execution_tasks:
                similar, regression = prepared.validation_groups(task)
                expected = manifest.validation_sets[task.task_id]
                self.assertEqual([item.task_id for item in similar], expected.similar)
                self.assertEqual([item.task_id for item in regression], expected.regression)
                self.assertNotIn(task.task_id, [item.task_id for item in (*similar, *regression)])
                row = next(row for row in rows if row["_id"] == task.task_id)
                score = prepared.score(execution().replace(task_id=task.task_id, output={
                    "answer": row["answer"], "supporting_facts": row["supporting_facts"]}), operation_id="official")
                self.assertEqual(score.score, 1)
                self.assertTrue(score.success)
                self.assertEqual(set(score.metrics), set(METRICS))
                exported = prepared.benchmark.export_predictions({task.task_id: {
                    "answer": row["answer"], "supporting_facts": row["supporting_facts"]}})
                self.assertEqual(exported["answer"][task.task_id], row["answer"])
                self.assertEqual(exported["sp"][task.task_id], row["supporting_facts"])
            # Cached rows must not expose mutable data shared by instances.
            copy = prepare_benchmark(path)
            self.assertEqual(copy.benchmark.dataset_version, prepared.benchmark.dataset_version)

    def test_manifest_rejects_cross_role_missing_and_duplicate_holdouts(self):
        with tempfile.TemporaryDirectory() as directory:
            _, _, manifest = fixture(Path(directory))
            current = next(iter(manifest.validation_sets))
            for values in ([current], ["missing"], manifest.validation_sets[current].regression,
                           manifest.validation_sets[current].similar * 2, []):
                data = manifest.model_dump()
                data["validation_sets"][current]["similar"] = values
                with self.subTest(values=values), self.assertRaises(ValueError):
                    TaskManifest.model_validate(data)
            data = manifest.model_dump()
            del data["validation_sets"][current]
            with self.assertRaises(ValueError):
                TaskManifest.model_validate(data)

    def test_selection_reproducible_and_disjoint(self):
        categories = {f"q{i}": str(i % 2) for i in range(30)}
        args = dict(seed=42, task_count=6, similar_count=2, regression_count=3, test_ids=["test"])
        first = select_holdouts(categories, **args)
        self.assertEqual(first, select_holdouts(dict(reversed(list(categories.items()))), **args))
        roles, sets = first
        for key, selection in sets.items():
            self.assertTrue(all(categories[item] == categories[key] for item in selection.similar))
            self.assertEqual(len(selection.regression), 3)
            self.assertEqual(len(set(selection.regression)), 3)
            self.assertTrue(all(roles[item] == "regression" for item in selection.regression))

    def test_source_mutation_invalidates_cached_parse(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, _, _ = config_file(root)
            prepare_benchmark(path)
            source = root / "train.json"
            source.write_text(source.read_text(encoding="utf-8") + " ", encoding="utf-8")
            with self.assertRaisesRegex(BenchmarkError, "checksum mismatch"):
                prepare_benchmark(path)

    def test_native_contract_rejects_outer_or_inner_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, _, _ = config_file(root)
            original = yaml.safe_load(path.read_text(encoding="utf-8"))
            import copy
            for plugin, settings, outer in [("deterministic_evaluator", {}, True),
                    ("deterministic_evaluator", {}, False), ("hotpotqa_evaluator", {"primary_metric": "f1"}, False)]:
                data = copy.deepcopy(original)
                if outer:
                    data["assessment"].update(evaluator=plugin, evaluator_config=settings)
                else:
                    data["targets"]["operator_configs"]["prompt"]["trial_evaluator"] = {"plugin": plugin, "config": settings}
                path.write_text(yaml.safe_dump(data), encoding="utf-8")
                with self.assertRaises(ContractError):
                    prepare_benchmark(path)
            original["targets"]["operator_configs"]["prompt"]["trial_evaluator"] = {"plugin": "hotpotqa_evaluator", "config": {}}
            path.write_text(yaml.safe_dump(original), encoding="utf-8")
            prepare_benchmark(path)  # Default-normalized equivalent settings are allowed.

    def test_manifest_changes_invalidate_validation_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, _, _ = config_file(root)
            first = prepare_benchmark(path)
            manifest_path = root / "manifest.json"
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            key = next(iter(data["validation_sets"]))
            data["validation_sets"][key]["similar"].reverse()
            manifest_path.write_text(json.dumps(data), encoding="utf-8")
            second = prepare_benchmark(path)
            self.assertNotEqual(first.benchmark.dataset_version, second.benchmark.dataset_version)

    def test_primary_metric_is_explicit_and_success_still_means_joint_em(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, rows, _ = config_file(root)
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            data["assessment"]["evaluator_config"]["primary_metric"] = "f1"
            path.write_text(yaml.safe_dump(data), encoding="utf-8")
            prepared = prepare_benchmark(path)
            task = prepared.execution_tasks[0]
            row = next(row for row in rows if row["_id"] == task.task_id)
            result = prepared.score(execution().replace(task_id=task.task_id,
                output={"answer": row["answer"], "supporting_facts": []}), operation_id="eval")
            self.assertEqual(result.score, 1)
            self.assertFalse(result.success)
