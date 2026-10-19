import json
import tempfile
import unittest
from pathlib import Path

from aer_core.contracts.benchmark import BenchmarkPlugin
from aer_core.errors import BenchmarkError, UnsupportedCapability
from plugins.benchmarks.toy_tasks import ToyBenchmark


DATA = Path(__file__).resolve().parents[2] / "plugins/benchmarks/toy_tasks"


class ToyBenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.benchmark = ToyBenchmark()

    def changed_dataset(self, change):
        public = json.loads((DATA / "tasks.json").read_text(encoding="utf-8"))
        private = json.loads((DATA / "scoring_specs.json").read_text(encoding="utf-8"))
        change(public, private)
        with tempfile.TemporaryDirectory() as directory:
            tasks = Path(directory) / "tasks.json"
            specs = Path(directory) / "specs.json"
            tasks.write_text(json.dumps(public), encoding="utf-8")
            specs.write_text(json.dumps(private), encoding="utf-8")
            return ToyBenchmark(tasks, specs)

    def test_stable_order_disjoint_splits_and_fixed_groups(self):
        self.assertIsInstance(self.benchmark, BenchmarkPlugin)
        again = ToyBenchmark()
        identities = []
        sources = []
        for split, size in [("dev", 4), ("validation", 8), ("test", 4)]:
            tasks = self.benchmark.tasks(split)
            self.assertEqual(len(tasks), size)
            self.assertEqual(tasks, again.tasks(split))
            identities.extend(task.identity for task in tasks)
            sources.extend(task.metadata["source_id"] for task in tasks)
            self.assertTrue(all(task.metadata["split"] == split for task in tasks))
        self.assertEqual(len(identities), len(set((x.benchmark_id, x.task_id) for x in identities)))
        self.assertEqual(len(sources), len(set(sources)))
        groups = [task.metadata["validation_group"] for task in self.benchmark.tasks("validation")]
        self.assertEqual(groups.count("similar"), 4)
        self.assertEqual(groups.count("regression"), 4)

    def test_public_views_contain_no_scoring_refs_or_answers(self):
        task = self.benchmark.tasks("dev")[0]
        view = self.benchmark.task_view(task)
        data = view.model_dump(mode="json")
        self.assertNotIn("eval_spec_ref", data)
        self.assertNotIn("private_parameters", data)
        self.assertNotIn("expected", json.dumps(data))
        self.assertNotIn("75", json.dumps(data))
        self.assertEqual(self.benchmark.evaluation_spec(task).private_parameters["expected"], 75)
        with self.assertRaises(TypeError):
            view.input["data"]["expression"] = "changed"
        self.assertEqual(self.benchmark.task_view(task), view)

    def test_unknown_split_and_foreign_or_modified_task_are_rejected(self):
        with self.assertRaises(UnsupportedCapability):
            self.benchmark.tasks("train")
        task = self.benchmark.tasks("dev")[0]
        for changed in [task.replace(benchmark_id="foreign"), task.replace(input={"instruction": "altered"}),
                        task.replace(eval_spec_ref="wrong")]:
            with self.subTest(task=changed):
                with self.assertRaises(BenchmarkError):
                    self.benchmark.evaluation_spec(changed)
                with self.assertRaises(BenchmarkError):
                    self.benchmark.task_view(changed)

    def test_index_tracks_all_real_tasks_without_scoring_data(self):
        index = self.benchmark.index()
        self.assertEqual(index.benchmark_id, "toy_tasks")
        self.assertEqual([x.task_id for x in index.splits["dev"]], [t.task_id for t in self.benchmark.tasks("dev")])
        encoded = index.model_dump_json()
        self.assertNotIn("private_parameters", encoded)
        self.assertNotIn("expected", encoded)
        self.assertNotIn("expression", encoded)

    def test_duplicate_identity_or_source_across_splits_is_rejected(self):
        for key in ("task_id", "source_id"):
            def change(public, private):
                public["tasks"][4][key] = public["tasks"][0][key]
            with self.subTest(key=key), self.assertRaisesRegex(BenchmarkError, "unique"):
                self.changed_dataset(change)

    def test_missing_or_extra_or_duplicate_spec_is_rejected(self):
        changes = [lambda p, s: s["specs"].pop(),
                   lambda p, s: s["specs"].append(s["specs"][0].copy()),
                   lambda p, s: s["specs"][0].update(task_id="absent")]
        for change in changes:
            with self.subTest(change=change), self.assertRaisesRegex(BenchmarkError, "one-to-one"):
                self.changed_dataset(change)

    def test_bad_group_or_empty_split_is_rejected(self):
        changes = [lambda p, s: p["tasks"][0].update(validation_group="similar"),
                   lambda p, s: p["tasks"][4].update(validation_group=None),
                   lambda p, s: [x.update(validation_group="similar") for x in p["tasks"] if x["split"] == "validation"],
                   lambda p, s: [x.update(split="dev") for x in p["tasks"] if x["split"] == "test"]]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(BenchmarkError):
                self.changed_dataset(change)

    def test_mismatched_versions_or_unsupported_scoring_is_rejected(self):
        changes = [lambda p, s: s.update(dataset_version="another-version"),
                   lambda p, s: s.update(scorer_version="unknown"),
                   lambda p, s: s["specs"][0].update(scoring_method="llm_judge")]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(BenchmarkError):
                self.changed_dataset(change)

    def test_hidden_format_requirement_or_tolerance_is_rejected(self):
        def hidden_extra(public, private):
            public["tasks"][2]["input"]["output_format"]["allow_extra_fields"] = True
            private["specs"][2]["private_parameters"]["expected"]["secret"] = "extra"
        changes = [lambda p, s: s["specs"][0]["private_parameters"].update(absolute_tolerance=0.5),
                   lambda p, s: s["specs"][2]["private_parameters"]["expected"].update(secret="extra"),
                   lambda p, s: s["specs"][2]["private_parameters"]["expected"].update(count="6"), hidden_extra]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(BenchmarkError):
                self.changed_dataset(change)

    def test_malformed_or_duplicate_key_json_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tasks.json"
            for text in ['{"benchmark_id":"toy_tasks","benchmark_id":"other"}', '{',
                         '{"value":NaN}']:
                path.write_text(text, encoding="utf-8")
                with self.subTest(text=text), self.assertRaises(BenchmarkError):
                    ToyBenchmark(path, DATA / "scoring_specs.json")
            with self.assertRaises(BenchmarkError):
                ToyBenchmark(Path(directory) / "missing.json")
