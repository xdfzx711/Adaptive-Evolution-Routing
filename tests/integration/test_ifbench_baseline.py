from contextlib import ExitStack
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

from aer_core.errors import ExecutionError, PluginConfigurationError
from experiments.batch import run_benchmark
from experiments.optimization import ConfiguredModelFactory
from plugins.evaluators.ifbench import IFBenchEvaluator, IFBenchEvaluatorSettings
from plugins.workspace.collectors import RawTextResultCollector
from tests.benchmarks.test_ifbench_selection import ROOT, fixture
from tests.execution.helpers import RecordingModel, context, response


class RawCollectorTests(unittest.TestCase):
    def test_raw_answer_is_preserved_including_empty_and_json_like_text(self):
        collector = RawTextResultCollector()
        self.assertIsNone(collector.schema(None))
        for text in ('', '  Hello\r\n\n', '```json\n{"x": 1}\n```'):
            self.assertEqual(collector.collect(None, None, text, context()).output['response'], text)
        with self.assertRaises(ExecutionError):
            collector.collect(None, None, None, context())


@unittest.skipUnless(importlib.util.find_spec('ifbench') and importlib.util.find_spec('gepa'), 'install [ifbench,gepa]')
class IFBenchBaselineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            IFBenchEvaluator(IFBenchEvaluatorSettings())
        except PluginConfigurationError as error:
            raise unittest.SkipTest(str(error))

    def config(self, root, tasks=2):
        _, manifest, _ = fixture(root, task_count=tasks)
        data = yaml.safe_load((ROOT / 'experiments/configs/v0_1_codex_ifbench_gepa_80_r3.yaml').read_text(encoding='utf-8'))
        data['benchmark']['config']['manifest_path'] = str(manifest)
        data['agent']['initial_prompt'] = 'BAD initial prompt'
        data['agent']['config']['command'] = [sys.executable, str(ROOT / 'tests/fixtures/ifbench_codex.py')]
        data['assessment']['diagnoser'] = 'lightweight_diagnoser'
        data['assessment']['evaluator_config']['nltk_data_path'] = str(ROOT / '.aer-cache/ifbench/nltk_data')
        provider = {'plugin': 'scripted_model', 'model_id': 'scripted-v0.1', 'config': {'scenario_id': 'mvp_v0_1_contract'}}
        data['providers']['model'] = provider
        data['targets']['operator_configs']['prompt'].update(max_iterations=1, max_metric_calls=6, reflection_model=provider)
        data['storage']['root'] = str(root / 'runs')
        path = root / 'config.yaml'
        path.write_text(yaml.safe_dump(data), encoding='utf-8')
        return path

    def test_real_gepa_raw_collector_official_scores_commit_and_holdouts(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            path = self.config(Path(directory))
            reflection = RecordingModel(lambda *_: response('```\nIMPROVED follow the request exactly\n```'))
            stack.enter_context(patch.object(ConfiguredModelFactory, 'create', return_value=reflection))
            summary = run_benchmark(path, report_holdouts=True)
            self.assertEqual(summary['status'], 'completed')
            self.assertEqual(len(summary['tasks']), 2)
            for task in summary['tasks']:
                self.assertEqual(task['initial_evaluation']['score'], 0)
                self.assertEqual(task['final_evaluation']['score'], 1)
                self.assertEqual(task['final_output'], {'response': 'Hello'})
                self.assertEqual(task['stop_reason'], 'task_succeeded')
                self.assertEqual(len(task['rounds']), 1)
                self.assertEqual(task['holdout_reporting']['status'], 'complete')
                self.assertTrue(task['rounds'][0]['candidates'][0]['committed'])
                record = json.loads((Path(summary['run_root']) / task['rounds'][0]['record']).read_text(encoding='utf-8'))
                trials = record['generation']['internal_optimization']['trials']
                self.assertEqual([t['evaluation']['score'] for t in trials], [0, 0, 0, 1, 1, 1])
                self.assertTrue(all(t['evaluation']['assessment_version'] == summary['scorer_version'] for t in trials))
                self.assertTrue(all(t['evaluation']['checks'] for t in trials))
                self.assertEqual(trials[0]['evaluation']['checks'][0]['status'], 'failed')
                self.assertEqual(trials[-1]['evaluation']['checks'][0]['status'], 'passed')
                self.assertIn(trials[0]['evaluation']['checks'][0]['check_id'],
                    record['generation']['internal_optimization']['reflections'][0]['request']['messages'][-1]['content'])
            predictions = json.loads(Path(summary['predictions_path']).read_text(encoding='utf-8'))
            self.assertEqual(len(predictions), 2)
            self.assertTrue(all(p['response'] == 'Hello' for p in predictions))


if __name__ == '__main__':
    unittest.main()
