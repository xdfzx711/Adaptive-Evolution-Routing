"""Offline real-Git integration: no Codex model, Docker, or upstream repository."""

import json
from pathlib import Path
import sys
import tempfile
import unittest

import yaml

from aer_core.errors import ConfigError, EvaluationError
from experiments.benchmark import prepare_benchmark
from experiments.configuration import load_experiment_config
from experiments.orchestration import prepare_orchestrator
from plugins.workspace.git import git
from plugins.workspace.local import LocalRepoWorkspace, LocalRepoSettings
from plugins.workspace.collectors import GitPatchCollector, PatchCollectorSettings
from plugins.mocks import MockArtifactStore
from tests.execution.helpers import context, MODEL
from tests.execution.test_codex import FAKE
from tests.integration.test_codex_routing import attach


ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "experiments/configs/v0_1_codex_swebench_static.yaml"
THREE_TASKS = ROOT / "experiments/configs/v0_1_codex_swebench_three_tasks.yaml"


def fixture(root):
    source = root / "source"
    source.mkdir()
    git(source, "init", "--quiet")
    (source / "module.py").write_text("answer = 1\n", encoding="utf-8")
    git(source, "add", ".")
    git(source, "-c", "user.name=Fixture", "-c", "user.email=fixture@localhost", "commit", "-qm", "base")
    revision = git(source, "rev-parse", "HEAD").strip()
    (source / "module.py").write_text("answer = 2\n", encoding="utf-8")
    patch = git(source, "diff")
    # Include a later commit with the answer: workspaces must not inherit it.
    git(source, "add", ".")
    git(source, "-c", "user.name=Fixture", "-c", "user.email=fixture@localhost", "commit", "-qm", "private future fix")
    instances = [{"instance_id": name, "repo": "fixture/repo", "base_commit": revision,
                  "problem_statement": "Change the public constant from one to two.", "patch": patch,
                  "test_patch": "PRIVATE_SCORING_TEST"} for name in ("dev-issue", "similar-issue", "regression-issue")]
    dataset = root / "data.json"
    dataset.write_text(json.dumps({"instances": instances}), encoding="utf-8")
    data = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    data["benchmark"]["task_ids"] = ["dev-issue"]
    data["benchmark"]["config"] = {"dataset_path": "data.json", "roles": {
        "dev-issue": "dev", "similar-issue": "similar", "regression-issue": "regression"}}
    data["agent"]["config"].update(command=[sys.executable, str(FAKE), "--fixture-mode", "patch"],
                                     workspace_root=str(root / "workspaces"))
    data["agent"]["config"]["workspace"]["config"] = {
        "cache_root": str(root / "cache"), "repositories": {"fixture/repo": str(source)}}
    data["storage"]["root"] = str(root / "runs")
    data["providers"]["model"] = MODEL.model_dump(mode="json")
    path = root / "config.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path, source, patch


class SwebenchStaticTests(unittest.TestCase):
    def test_expanded_config_has_three_disjoint_validation_sets_and_five_rounds(self):
        prepared = prepare_benchmark(THREE_TASKS)
        self.assertEqual(prepared.loaded_config.config.limits.max_evolution_rounds, 5)
        self.assertEqual(prepared.loaded_config.config.state.task_mode, "independent")
        self.assertEqual([len(prepared.execution_tasks), len(prepared.similar_tasks), len(prepared.regression_tasks)], [3, 3, 3])
        seen = {task.task_id for task in prepared.execution_tasks}
        inspection = prepared.as_dict()["validation_tasks_by_execution_task"]
        for task in prepared.execution_tasks:
            similar, regression = prepared.validation_groups(task)
            self.assertEqual((len(similar), len(regression)), (1, 1))
            self.assertEqual(inspection[task.task_id], {"similar": [similar[0].task_id], "regression": [regression[0].task_id]})
            for item in (*similar, *regression):
                self.assertNotIn(item.task_id, seen)
                self.assertEqual(item.metadata["repo"], task.metadata["repo"])
                seen.add(item.task_id)
        self.assertEqual(len(seen), 9)

    def test_invalid_task_specific_validation_is_rejected_during_config_loading(self):
        overrides = [
            "benchmark.config.validation_sets.psf__requests-1963.similar=[]",
            "benchmark.config.validation_sets.psf__requests-1963.similar=[missing]",
            "benchmark.config.validation_sets.psf__requests-1963.similar=[psf__requests-1963]",
            "benchmark.config.validation_sets.psf__requests-1963.similar=[psf__requests-2317]",
            "benchmark.config.validation_sets.psf__requests-1963.similar=[psf__requests-2148,psf__requests-2148]",
            "benchmark.config.validation_sets={psf__requests-1963: {similar: [psf__requests-2148], regression: [psf__requests-2317]}}",
        ]
        for override in overrides:
            with self.subTest(override=override), self.assertRaises(ConfigError):
                load_experiment_config(THREE_TASKS, overrides=[override])

    def test_three_episodes_use_only_their_own_pairs_and_allow_five_rejected_rounds(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, _, _ = fixture(root)
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            dataset_path = root / "data.json"
            template = json.loads(dataset_path.read_text(encoding="utf-8"))["instances"][0]
            roles, selections, instances = {}, {}, []
            for number in range(1, 4):
                main_id = f"dev-{number}"
                selections[main_id] = {"similar": [f"similar-{number}"], "regression": [f"regression-{number}"]}
                for role in ("dev", "similar", "regression"):
                    task_id = f"{role}-{number}"
                    roles[task_id] = role
                    instances.append({**template, "instance_id": task_id})
            dataset_path.write_text(json.dumps({"instances": instances}), encoding="utf-8")
            data["benchmark"]["task_ids"] = list(selections)
            data["benchmark"]["config"].update(roles=roles, validation_sets=selections)
            data["limits"]["max_evolution_rounds"] = 5
            path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
            startup = prepare_orchestrator(path)
            initial_version_id = None
            for number in range(1, 4):
                task = startup.prepare_task(f"dev-{number}")
                initial = task.execution.storage.version_store.stable()
                if initial_version_id is None:
                    initial_version_id = initial.version_id
                self.assertEqual(initial.version_id, initial_version_id)
                attach(task, choices=("prompt",) * 5 if number == 1 else ("model",))
                result = task.run()
                self.assertEqual(result.status, "completed", result.error)
                if number == 1:
                    self.assertEqual(result.evolution_rounds_used, 5)
                    self.assertEqual(result.stop_reason, "max_evolution_rounds_reached")
                    self.assertEqual(result.final_stable_version_id, initial.version_id)
                else:
                    self.assertEqual(result.evolution_rounds_used, 1)
                    self.assertEqual(result.stop_reason, "task_succeeded")
                    self.assertNotEqual(result.final_stable_version_id, initial.version_id)
                # A later independent task must start from the original model.
                self.assertEqual(result.initial_version_id, initial.version_id)
                saved = startup.save_record(result)
                records = list(Path(saved["run_root"]).glob(f"rounds/dev-{number}-*/round-*/record.json"))
                self.assertEqual(len(records), result.evolution_rounds_used)
                for record in records:
                    pairs = json.loads(record.read_text(encoding="utf-8"))["candidates"][0]["validation"]["tasks"]
                    observed = {(item["group"], item["task"]) for item in pairs}
                    self.assertEqual(observed, {("local", f"dev-{number}"), ("similar", f"similar-{number}"),
                                                ("regression", f"regression-{number}")})

    def test_packaged_three_instances_and_inspection_never_expose_scoring_fields(self):
        prepared = prepare_benchmark(CONFIG)
        self.assertEqual([len(prepared.execution_tasks), len(prepared.similar_tasks), len(prepared.regression_tasks)], [1, 1, 1])
        public = json.dumps(prepared.as_dict())
        for private in ("reference_patch", "test_patch", "private_parameters", "FAIL_TO_PASS", "PASS_TO_PASS"):
            self.assertNotIn(private, public)
        self.assertEqual(prepared.benchmark.tasks("test"), ())

    def test_new_plugins_build_from_configuration_and_complete_validation_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, source, reference = fixture(root)
            startup = prepare_orchestrator(path, console=False)
            self.assertIn("workspace_provider", startup.execution.built_plugins.instances)
            self.assertIn("result_collector", startup.execution.built_plugins.instances)
            task = startup.prepare_task("dev-issue")
            model = attach(task)
            result = task.run()
            self.assertEqual(result.status, "completed", result.error)
            self.assertEqual(result.stop_reason, "task_succeeded")
            self.assertEqual(result.evolution_rounds_used, 1)
            self.assertEqual(result.final_evaluation.score, 1.0)
            execution = result.final_execution
            self.assertEqual(execution.state_summary["evaluation_mode"], "static")
            self.assertEqual(tuple(execution.output["changed_files"]), ("module.py",))
            self.assertEqual(execution.artifact_refs[0].kind, "code_patch")
            self.assertEqual(task.execution.storage.artifact_store.get(execution.artifact_refs[0]).decode(), execution.output["patch"])
            self.assertEqual((source / "module.py").read_text(), "answer = 2\n")
            self.assertEqual(result.final_evaluation.metrics["tests_executed"], 0.0)
            for request, ctx in model.requests:
                public = "\n".join(message.content or "" for message in request.messages)
                self.assertNotIn("reference_patch", public)
                self.assertNotIn("PRIVATE_SCORING_TEST", public)
            # Every execution starts from base, even after a candidate commits.
            workspaces = list((root / "workspaces").iterdir())
            self.assertEqual(len(workspaces), 6)
            for workspace in workspaces:
                self.assertEqual(git(workspace, "log", "--format=%s").strip(), "Task base tree")
                self.assertEqual(git(workspace, "remote").strip(), "")
                self.assertEqual(git(workspace, "show", "HEAD:module.py").replace("\r\n", "\n"), "answer = 1\n")

    def test_workspace_is_fresh_and_collector_sees_staged_new_files_and_syntax_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, source, _ = fixture(root)
            prepared = prepare_benchmark(path)
            task = prepared.benchmark.task_view(prepared.execution_tasks[0])
            provider = LocalRepoWorkspace(LocalRepoSettings(cache_root=str(root / "cache"),
                repositories={"fixture/repo": str(source)}), root)
            first = provider.prepare(task, root / "first", context())
            second = provider.prepare(task, root / "second", context())
            self.assertEqual((first.path / "module.py").read_text(), "answer = 1\n")
            (first.path / "new.py").write_text("raise RuntimeError('MUST_NOT_EXECUTE')\n", encoding="utf-8")
            (first.path / "module.py").write_text("answer = 2\n", encoding="utf-8")
            git(first.path, "add", "module.py")
            collector = GitPatchCollector(PatchCollectorSettings(), MockArtifactStore())
            result = collector.collect(task, first, "untrusted summary", context())
            self.assertEqual(tuple(result.output["changed_files"]), ("module.py", "new.py"))
            self.assertTrue(result.output["syntax_valid"])
            self.assertEqual((second.path / "module.py").read_text(), "answer = 1\n")
            (first.path / "module.py").write_text("def broken(:\n", encoding="utf-8")
            result = collector.collect(task, first, "", context())
            self.assertFalse(result.output["syntax_valid"])
            self.assertEqual(tuple(result.output["syntax_error_files"]), ("module.py",))

    def test_static_scores_reference_partial_empty_invalid_and_wrong_identity(self):
        from tests.domain_helpers import execution
        with tempfile.TemporaryDirectory() as directory:
            path, _, reference = fixture(Path(directory))
            prepared = prepare_benchmark(path)
            task = prepared.execution_tasks[0]
            spec = prepared.benchmark.evaluation_spec(task)
            def grade(patch, **values):
                result = execution().replace(task_id=task.task_id, output={"patch": patch, "patch_valid": True,
                    "syntax_valid": True, **values})
                return prepared.evaluator.evaluate(task, result, spec, context())
            self.assertEqual(grade(reference).score, 1.0)
            # Diff line coordinates do not change the static similarity.
            self.assertEqual(grade(reference.replace("@@ -1 +1 @@", "@@ -9 +9 @@")).score, 1.0)
            partial = grade(reference.replace("+answer = 2", "+answer = 3"))
            self.assertGreater(partial.score, 0)
            self.assertLess(partial.score, 1)
            self.assertFalse(partial.success)
            self.assertEqual(grade("").score, 0)
            self.assertEqual(grade("not a patch").reason_code, "invalid_patch")
            self.assertEqual(grade(reference, syntax_valid=False).reason_code, "syntax_error")
            with self.assertRaises(EvaluationError):
                prepared.evaluator.evaluate(task, execution(), spec, context())

    def test_bad_plugin_references_and_configuration_fail_before_execution(self):
        for field, value in (("workspace.plugin", "missing"), ("collector.plugin", "missing"),
                             ("workspace.config.git_timeout_seconds", -1)):
            with self.subTest(field=field), self.assertRaises(ConfigError):
                load_experiment_config(CONFIG, overrides=[f"agent.config.{field}={value}"])
        with self.assertRaises(ConfigError):
            load_experiment_config(CONFIG, overrides=["assessment.evaluator_config.success_threshold=2.0"])
