import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml

from aer_core.errors import ContractError
from aer_core.schemas.execution import ExecutionResult
from aer_core.schemas.usage import ResourceUsage
from aer_core.schemas.validation import PairSeed
from aer_core.task_executor import ScoredTaskExecutor
from experiments.benchmark import prepare_benchmark
from experiments.configuration import load_experiment_config
from experiments.evolution import prepare_evolution
from experiments.environment import env_file_paths
from experiments.optimization import ConfiguredModelFactory
from plugins.evaluators.llm_patch import LLMPatchConfig, LLMPatchEvaluator
from plugins.validation.acceptance import ImprovementWithRegressionGuard
from plugins.validation.cache import LocalValidationCache
from plugins.validation.paired import PairedValidation
from plugins.workspace.local import LocalRepoSettings, LocalRepoWorkspace
from tests.benchmarks.test_llm_patch import verdict
from tests.execution.helpers import MODEL, RecordingModel, context, response
from tests.integration.test_swebench_static import fixture as repository_fixture
from tests.validation.helpers import fixture as version_fixture, settings


ROOT = Path(__file__).resolve().parents[2]


class LLMPatchCompositionTests(unittest.TestCase):
    def test_judge_provider_resolves_its_own_environment_without_changing_main_model(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, _, _ = repository_fixture(root)
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            original = data["providers"]["model"]
            env = root / "judge.env"
            env.write_text("JUDGE_MODEL=judge-only\nJUDGE_KEY=fixture-key\nJUDGE_URL=https://judge.example/v1\n", encoding="utf-8")
            data["assessment"].update(evaluator="llm_patch_evaluator", evaluator_config={"judge_model": {
                "plugin": "qwen_model", "model_id": "${JUDGE_MODEL}", "config": {"dotenv_path": "judge.env",
                    "api_key_env": "JUDGE_KEY", "base_url_env": "JUDGE_URL", "temperature": 0.0}}})
            path.write_text(yaml.safe_dump(data), encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True):
                prepared = prepare_benchmark(path)
            self.assertEqual(prepared.evaluator.model.model_id, "judge-only")
            self.assertEqual(prepared.evaluator.model.config.temperature, 0)
            self.assertEqual(prepared.loaded_config.config.providers.model.model_dump(mode="json"), original)
            self.assertIn(env, env_file_paths(prepared.loaded_config))
            self.assertNotIn("fixture-key", prepared.loaded_config.config.model_dump_json())

    def test_run_config_selects_jev_and_outer_scorer_and_pins_inner_profile(self):
        base = yaml.safe_load((ROOT / "experiments/configs/v0_1_codex_swebench_gepa.yaml").read_text(encoding="utf-8"))
        changed = yaml.safe_load((ROOT / "experiments/configs/v0_1_codex_swebench_gepa_llm_eval.yaml").read_text(encoding="utf-8"))
        profile = changed["targets"]["operator_configs"]["prompt"].pop("trial_evaluator")
        self.assertEqual(profile, {"plugin": base["assessment"]["evaluator"], "config": base["assessment"]["evaluator_config"]})
        self.assertEqual(changed["routing"]["plugin"], "jev_router")
        changed["routing"] = base["routing"]
        changed["assessment"] = base["assessment"]
        self.assertEqual(base, changed)
        loaded = load_experiment_config(ROOT / "experiments/configs/v0_1_codex_swebench_gepa_llm_eval.yaml")
        self.assertEqual(loaded.config.assessment.evaluator, "llm_patch_evaluator")
        self.assertEqual(loaded.config.targets.allowed, ["prompt"])

    @unittest.skipUnless(importlib.util.find_spec("gepa"), "install the [gepa] extra")
    def test_gepa_uses_static_scores_without_calling_outer_judge(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, _, _ = repository_fixture(root)
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            data["agent"]["config"]["initial_model"] = "gpt-6.1-sol"
            data["assessment"].update(evaluator="llm_patch_evaluator", evaluator_config={
                "judge_model": MODEL.model_copy(update={"model_id": "judge-fixture"}).model_dump(mode="json")})
            data["targets"].update(allowed=["prompt"], default_operators={"prompt": "gepa_prompt_evolver"},
                operator_configs={"prompt": {"max_metric_calls": 6, "max_iterations": 1,
                    "reflection_model": MODEL.model_dump(mode="json"),
                    "trial_evaluator": {"plugin": "static_patch_evaluator", "config": {"changed_lines_weight": 0.8, "success_threshold": 1.0}}}})
            data["evolution"]["mode"] = "fixed"
            data["routing"].update(plugin="fixed_router", fixed_target="prompt")
            path.write_text(yaml.safe_dump(data), encoding="utf-8")
            judge = RecordingModel(lambda *_: response(json.dumps(verdict("partial"))))
            reflection = RecordingModel(lambda *_: response("Some reusable instructions"))
            def factory(_, settings):
                return judge if settings.model_id == "judge-fixture" else reflection
            with patch.object(ConfiguredModelFactory, "create", factory):
                prepared = prepare_evolution(path, run_id="separate-scorers")
            task = prepared.execution.benchmark.execution_tasks[0]
            stable = prepared.execution.storage.version_store.stable()
            ctx = context(workspace_ref=str(prepared.execution.storage.run_root), operation_id="outer-score")
            outer = ScoredTaskExecutor(prepared.execution.agent, prepared.execution.benchmark.benchmark,
                prepared.execution.benchmark.evaluator).execute(task, stable, ctx)
            self.assertTrue(outer.complete, outer.error)
            self.assertEqual(outer.evaluation.score, 0.5)
            operator = prepared.resolver.resolve("prompt").operator
            self.assertEqual(operator.trials.evaluator_settings.plugin, "static_patch_evaluator")
            observed = operator.trials.evaluate(task.identity, task.identity, stable, "Trial instructions", ctx)
            self.assertIsNone(observed.error)
            self.assertEqual(observed.feedback.score, 1)
            self.assertEqual(observed.feedback.public_feedback,
                "Static comparison met the threshold. This does not establish test correctness.")
            self.assertEqual(len(judge.requests), 1)
            self.assertEqual(prepared.execution.agent.snapshot(), stable)
            self.assertEqual(prepared.execution.benchmark.loaded_config.config.assessment.evaluator, "llm_patch_evaluator")
            with self.assertRaises(ContractError):
                operator.trials.tasks(task.identity, ("similar-issue",))


class LLMPatchPairedValidationTests(unittest.TestCase):
    def test_similar_gain_is_accepted_and_regression_drop_is_rejected(self):
        for drop, expected in ((False, True), (True, False)):
            with self.subTest(drop=drop), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                config_path, source, reference = repository_fixture(root)
                benchmark = prepare_benchmark(config_path).benchmark
                current = benchmark.tasks("dev")[0]
                similar, regression = benchmark.tasks("validation")
                artifacts, versions, stable, candidate, plan = version_fixture(root / "run")
                plan = plan.replace(current_task=current, similar_tasks=[similar], regression_tasks=[regression],
                    pair_seeds=[PairSeed(task=similar.identity, seed=7), PairSeed(task=regression.identity, seed=8)])
                class Agent:
                    def run(self, task, snapshot, run_ctx):
                        improved = snapshot.parent_version_id is not None
                        is_reg = task.task_id == regression.task_id
                        value = 3 if improved and is_reg and drop else 2 if improved or is_reg else 0
                        return ExecutionResult(task_id=task.task_id, agent_version_id=snapshot.version_id,
                            operation_id=run_ctx.operation_id, output={"patch": reference.replace("+answer = 2", f"+answer = {value}"),
                                "patch_valid": True, "syntax_valid": True}, trajectory=[], state_summary={},
                            status="completed", usage=ResourceUsage.zero())
                def review(query, ctx):
                    after = json.loads(query.messages[-1].content)["files"][0]["after"].strip()
                    label = "resolved" if after == "answer = 2" else "partial" if after == "answer = 3" else "unresolved"
                    return response(json.dumps(verdict(label, snippet=after)))
                judge = RecordingModel(review)
                workspace = LocalRepoWorkspace(LocalRepoSettings(cache_root=str(root / "cache"),
                    repositories={"fixture/repo": str(source)}), root)
                evaluator = LLMPatchEvaluator(LLMPatchConfig(judge_model=MODEL), judge, workspace)
                executor = ScoredTaskExecutor(Agent(), benchmark, evaluator)
                harness = PairedValidation(executor, artifacts, versions, LocalValidationCache(root / "run"),
                    {"scorer_version": evaluator.scorer_version})
                report = harness.validate(stable, [candidate], plan,
                    context(workspace_ref=str(root / "run"), operation_id="paired-judge"))[0]
                self.assertEqual(report.status, "complete", report.error)
                self.assertEqual(report.local_metrics.candidate_score, 1)
                self.assertEqual(report.candidate_metrics.similar_score, 1)
                self.assertEqual(report.candidate_metrics.regression_score, 0.5 if drop else 1)
                acceptance = ImprovementWithRegressionGuard(settings()).evaluate(report)
                self.assertEqual(acceptance.accepted, expected)
                self.assertEqual(acceptance.reason, "regression_guard" if drop else "improved")
                self.assertEqual(len(judge.requests), 6)
                self.assertEqual(report.usage.model_calls, 6)
                self.assertEqual(versions.stable(), stable)
