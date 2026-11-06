import copy
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import random
import unittest
from unittest.mock import patch

from aer_core.errors import EvaluationError, PluginConfigurationError
from aer_core.scoring import configure_scoring
from aer_core.schemas.scoring import ScoringSettings
from aer_core.schemas.task import EvaluationSpec
from plugins.evaluators.ifbench import IFBenchEvaluator, IFBenchEvaluatorSettings, require_ifbench, score_response
from tests.domain_helpers import task, execution, run_context


class OfficialSourceTests(unittest.TestCase):
    def test_vendored_source_matches_pinned_original(self):
        root = Path(__file__).resolve().parents[2] / "plugins/evaluators/ifbench_official"
        provenance = json.loads((root / "PROVENANCE.json").read_text(encoding="utf-8"))
        for name, expected in provenance["files"].items():
            self.assertEqual(sha256((root / name).read_bytes()).hexdigest(), expected)

    def test_registry_does_not_import_optional_ifbench(self):
        import subprocess, sys
        result = subprocess.run([sys.executable, "-c",
            "import sys; from experiments.bootstrap import create_registry; "
            "create_registry().get('evaluator', 'ifbench_evaluator'); "
            "assert 'ifbench' not in sys.modules; assert 'nltk' not in sys.modules"],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


@unittest.skipUnless(importlib.util.find_spec("ifbench"), "install optional [ifbench]")
class IFBenchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.evaluator = IFBenchEvaluator(IFBenchEvaluatorSettings())
        except PluginConfigurationError as error:
            raise unittest.SkipTest(str(error))

    def spec(self, ids=("format:no_whitespace",), kwargs=None):
        return EvaluationSpec(task=task().identity, scoring_method="ifbench.official.v1",
            private_parameters={"key": 1, "prompt": "Follow the supplied format.",
                "instruction_id_list": list(ids), "kwargs": kwargs or [{} for _ in ids]})

    def test_strict_loose_raw_whitespace_and_partial_constraints(self):
        spec = self.spec(("format:no_whitespace", "format:output_template"))
        result = self.evaluator.evaluate(task(), execution().replace(output={"response": "Hello"}), spec, run_context())
        self.assertEqual(result.score, 0)
        self.assertFalse(result.success)
        self.assertEqual(result.metrics["instruction_level_loose"], .5)
        self.assertEqual(result.metrics["constraint.0.format:no_whitespace.loose"], 1)
        projected = configure_scoring(self.evaluator, ScoringSettings(metric="instruction_level_loose"))
        result = projected.evaluate(task(), execution().replace(output={"response": "Hello"}), spec, run_context())
        self.assertEqual(result.score, .5)
        self.assertFalse(result.success)
        response = "explanation\nHello\n"
        result = self.evaluator.evaluate(task(), execution().replace(output={"response": response}), self.spec(), run_context())
        self.assertEqual(result.metrics["prompt_level_strict"], 0)
        self.assertEqual(result.metrics["prompt_level_loose"], 1)

    def test_official_parity_on_all_bundled_tasks(self):
        import ifbench
        rows = [json.loads(line) for line in ifbench.data_path().read_text(encoding="utf-8").splitlines() if line]
        official = self.evaluator.official
        self.assertEqual(len(rows), 300)
        state = random.getstate()
        try:
            for row in rows:
                with self.subTest(key=row["key"]):
                    response = "Hello World.\nThis is an example response with 123 numbers and *formatting*."
                    random.seed(42)
                    inp = official.InputExample(**copy.deepcopy(row))
                    strict = official.test_instruction_following_strict(inp, {row["prompt"]: response})
                    loose = official.test_instruction_following_loose(inp, {row["prompt"]: response})
                    random.seed(42)
                    metrics = score_response(response, row, official)
                    self.assertEqual(metrics["prompt_level_strict"], float(strict.follow_all_instructions))
                    self.assertEqual(metrics["prompt_level_loose"], float(loose.follow_all_instructions))
                    self.assertEqual(metrics["instruction_strict_passed"], sum(strict.follow_instruction_list))
                    self.assertEqual(metrics["instruction_loose_passed"], sum(loose.follow_instruction_list))
        finally:
            random.setstate(state)

    def test_none_kwargs_do_not_mutate_spec_and_missing_output_is_scored(self):
        spec = self.spec(kwargs=[{"unused": None}])
        before = spec.model_dump_json()
        for output in ({}, {"response": ""}, {"response": None}):
            result = self.evaluator.evaluate(task(), execution().replace(output=output), spec, run_context())
            self.assertEqual(result.score, 0)
        result = self.evaluator.evaluate(task(), execution().replace(output={"response": "Hello"}), spec, run_context())
        self.assertEqual(result.score, 1)
        self.assertEqual(spec.model_dump_json(), before)

    def test_checker_errors_are_not_zero_scores_and_missing_resources_fail_before_import(self):
        with patch.object(self.evaluator.official, "test_instruction_following_strict", side_effect=LookupError("resource")):
            with self.assertRaises(EvaluationError):
                self.evaluator.evaluate(task(), execution(), self.spec(), run_context())
        import nltk
        with patch.object(nltk.data, "find", side_effect=LookupError("missing")), patch.object(nltk, "download") as download:
            with self.assertRaises(PluginConfigurationError):
                require_ifbench()
            download.assert_not_called()


if __name__ == "__main__":
    unittest.main()
