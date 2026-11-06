from collections import Counter
import copy
import json
from pathlib import Path
import tempfile
import unittest

from aer_core.errors import BenchmarkError
from experiments.prepare_ifbench import prepare_manifest, prepare_source, read_rows, SOURCE_SHA256
from plugins.benchmarks.ifbench import IFBenchBenchmark, IFBenchSettings
from plugins.benchmarks.ifbench_selection import select_episodes, features
from plugins.benchmarks.manifest import file_digest


ROOT = Path(__file__).resolve().parents[2]


def fixture(root, task_count=2):
    rows = [{"key": f"{family}-{index}", "prompt": f"Request {family} {index}: answer Hello.",
        "instruction_id_list": [instruction], "kwargs": [kwargs]} for family, instruction, kwargs in (
            ("format", "format:no_whitespace", {}),
            ("length", "length_constraints:number_words", {"num_words": 2, "relation": "less than"}))
        for index in range(6)]
    source = root / "source.jsonl"
    source.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    path = root / "manifest.json"
    manifest = prepare_manifest(source, path, task_count=task_count)
    return rows, path, manifest


class SelectionTests(unittest.TestCase):
    def test_exact_pairs_disjoint_families_reproducible_and_label_independent(self):
        with tempfile.TemporaryDirectory() as directory:
            rows, _, manifest = fixture(Path(directory))
            roles, selections, policy = select_episodes(list(reversed(rows)), task_count=2)
            self.assertEqual((roles, selections, policy), (manifest.roles, manifest.validation_sets, manifest.selection_policy))
            changed = copy.deepcopy(rows)
            for row in changed:
                row['kwargs'] = [{"unseen_parameter": "not used by selection"}]
            self.assertEqual(select_episodes(changed, task_count=2), (roles, selections, policy))
            self.assertEqual(Counter(roles.values()), {"dev": 2, "similar": 2, "regression": 2})
            by_id = {r['key']: r for r in rows}
            for key, pair in selections.items():
                a, b, c = [features(by_id[k]) for k in (key, pair.similar[0], pair.regression[0])]
                self.assertEqual(a['constraints'], b['constraints'])
                self.assertEqual(a['count'], c['count'])
                self.assertFalse(set(a['families']) & set(c['families']))

    def test_insufficient_or_duplicate_sources_fail_without_relaxing_rules(self):
        with tempfile.TemporaryDirectory() as directory:
            rows, _, _ = fixture(Path(directory))
            with self.assertRaises(ValueError):
                select_episodes(rows, task_count=5)
            with self.assertRaises(ValueError):
                select_episodes(rows + [rows[0]], task_count=2)
            for row in rows:
                row['prompt'] = 'same prompt'
            with self.assertRaises(ValueError):
                select_episodes(rows, task_count=2)

    def test_adapter_private_labels_export_and_manifest_tampering(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, path, manifest = fixture(root)
            benchmark = IFBenchBenchmark(IFBenchSettings(manifest_path=str(path)), root)
            task = benchmark.tasks('dev')[0]
            view = benchmark.task_view(task)
            self.assertEqual(set(view.input), {'prompt'})
            self.assertNotIn('kwargs', view.model_dump_json())
            self.assertNotIn('instruction_id_list', view.model_dump_json())
            self.assertEqual(len(benchmark.validation_tasks(task)), 2)
            self.assertFalse(benchmark.feedback_policy(benchmark.validation_tasks(task)[0]).allow_evolution)
            raw = '  Hello\n```\n'
            self.assertEqual(benchmark.export_predictions({task.task_id: {'response': raw}})[0]['response'], raw)
            self.assertEqual(benchmark.tasks('test'), ())
            data = manifest.model_dump(mode='json')
            data['selection_policy']['assignments'][task.task_id]['current']['count'] = 999
            path.write_text(json.dumps(data), encoding='utf-8')
            with self.assertRaisesRegex(BenchmarkError, 'selection'):
                IFBenchBenchmark(IFBenchSettings(manifest_path=str(path)), root)
            data = manifest.model_dump(mode='json')
            data['roles'] = dict(reversed(list(data['roles'].items())))
            path.write_text(json.dumps(data), encoding='utf-8')
            with self.assertRaisesRegex(BenchmarkError, 'selection'):
                IFBenchBenchmark(IFBenchSettings(manifest_path=str(path)), root)
            path.write_text(manifest.model_dump_json(), encoding='utf-8')
            (root / 'source.jsonl').write_text('{}', encoding='utf-8')
            with self.assertRaisesRegex(BenchmarkError, 'checksum'):
                IFBenchBenchmark(IFBenchSettings(manifest_path=str(path)), root)

    def test_checked_in_80_manifest_matches_pinned_source(self):
        from importlib.util import find_spec
        if find_spec('ifbench') is None:
            self.skipTest('install [ifbench] for pinned source audit')
        with tempfile.TemporaryDirectory() as directory:
            source = prepare_source(Path(directory))
            self.assertEqual(file_digest(source), SOURCE_SHA256)
            rows = read_rows(source)
            roles, selections, policy = select_episodes(rows)
            from plugins.benchmarks.manifest import TaskManifest
            manifest = TaskManifest.read(ROOT / 'experiments/data/ifbench_80_manifest.json')
            self.assertEqual((roles, selections, policy), (manifest.roles, manifest.validation_sets, manifest.selection_policy))
            self.assertEqual(len(roles), 240)
            self.assertEqual(len(policy['unused_ids']), 60)
            self.assertEqual(policy['constraint_count_quotas'], {'1': 68, '2': 12})


if __name__ == '__main__':
    unittest.main()
