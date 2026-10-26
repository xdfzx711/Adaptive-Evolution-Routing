import json
import os
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from aer_core.config.models import AgentSettings
from aer_core.schemas.references import AgentSnapshot
from aer_core.usage import sum_usage
from plugins.agents.codex import CodexCliAgent, CodexConfig, controlled_config, retryable_startup_failure
from plugins.agents.model_selection import CodexModelAdapter, ModelSelection
from plugins.agents.tool_selection import CodexToolAdapter
from plugins.tool_selection import ToolSelection
from plugins.agents.output import PublicJsonOutput
from aer_core.schemas.task import TaskView
from plugins.benchmarks.toy_tasks import ToyBenchmark
from plugins.evaluators.deterministic import DeterministicEvaluator
from plugins.mocks import MockArtifactStore
from tests.execution.helpers import context


FAKE = Path(__file__).with_name("fake_codex.py")


def fixture(root, mode="model", initial="gpt-5.6-sol", **changes):
    command = [sys.executable, str(FAKE), "--fixture-mode", mode]
    if mode.startswith("startup-"):
        command += ["--fixture-state-file", str(root / "startup-count.txt")]
    config = CodexConfig(command=command,
        workspace_root=str(root / "workspaces"), initial_model=initial, **changes)
    settings = AgentSettings(plugin="codex_cli_agent", initial_prompt="Use public evidence", initial_snapshot_ref=None,
                             config=config.model_dump(mode="json"))
    store = MockArtifactStore()
    artifacts = {"prompt": store.put(b"Use public evidence", "prompt"),
                   "model": store.put(CodexModelAdapter().encode(ModelSelection(model_id=initial,
                       reasoning_effort=config.initial_reasoning_effort)), "model")}
    if config.tooling is not None:
        artifacts["tool"] = store.put(CodexToolAdapter().encode(ToolSelection(enabled_tools=config.initial_tools or [])), "tool")
    snapshot = AgentSnapshot(version_id="v0", agent_plugin_id="codex_cli_agent", config=controlled_config(config), artifacts=artifacts)
    return CodexCliAgent(settings, snapshot, store), snapshot, store


class CodexTests(unittest.TestCase):
    def test_selected_tools_must_be_discovered_but_do_not_have_to_be_called(self):
        from tests.tools.helpers import LOCAL_CATALOG
        for mode, expected in (("tools-ignored", "error"), ("tools-unused", "completed")):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                agent, snapshot, _ = fixture(Path(directory), mode=mode,
                    tooling={"catalog": LOCAL_CATALOG}, initial_tools=["python_syntax_check"])
                benchmark = ToyBenchmark()
                result = agent.run(benchmark.task_view(benchmark.tasks("dev")[0]), snapshot, context())
                self.assertEqual(result.status, expected, result.error)
                self.assertEqual(result.state_summary["tool_invocations"], ())
                if mode == "tools-unused":
                    self.assertEqual(result.state_summary["available_tools"], ("python_syntax_check",))
                else:
                    self.assertIn("not available", result.error.message)
                    self.assertEqual(result.state_summary["tool_setup_status"], "not_started")
                    self.assertEqual(result.state_summary["tool_setup_error"], "bridge_did_not_start")

    def test_unknown_snapshot_tool_is_rejected_before_launching_codex(self):
        from tests.tools.helpers import LOCAL_CATALOG
        with tempfile.TemporaryDirectory() as directory:
            agent, snapshot, store = fixture(Path(directory), tooling={"catalog": LOCAL_CATALOG})
            bad = snapshot.replace(artifacts={**snapshot.artifacts,
                "tool": store.put(b'{"enabled_tools":["unknown"]}', "tool")})
            benchmark = ToyBenchmark()
            result = agent.run(benchmark.task_view(benchmark.tasks("dev")[0]), bad, context())
            self.assertEqual(result.status, "error")
            self.assertEqual(result.usage.model_calls, 0)
            self.assertEqual(agent.snapshot(), snapshot)

    def test_snapshot_effort_controls_actual_cli_execution_without_mutating_the_agent(self):
        with tempfile.TemporaryDirectory() as directory:
            agent, stable, artifacts = fixture(Path(directory), mode="reasoning",
                allowed_models=["gpt-5.6-sol"], initial_reasoning_effort="medium",
                allowed_reasoning_efforts={"gpt-5.6-sol": ["medium", "high"]})
            ref = artifacts.put(CodexModelAdapter().encode(ModelSelection(model_id="gpt-5.6-sol",
                reasoning_effort="high")), "model")
            candidate = stable.replace(version_id="v1", parent_version_id="v0",
                artifacts={**stable.artifacts, "model": ref})
            benchmark = ToyBenchmark()
            task = benchmark.tasks("dev")[0]
            evaluator = DeterministicEvaluator()
            for snapshot, effort, success in ((stable, "medium", False), (candidate, "high", True), (stable, "medium", False)):
                result = agent.run(benchmark.task_view(task), snapshot, context())
                self.assertEqual(result.status, "completed", result.error)
                self.assertEqual(result.state_summary["execution_reasoning_effort"], effort)
                self.assertEqual(result.trajectory[0].payload["request"]["reasoning_effort"], effort)
                self.assertEqual(evaluator.evaluate_record(task, result, benchmark.evaluation_spec(task),
                    operation_id="effort-eval").success, success)
            self.assertEqual(agent.snapshot(), stable)
            self.assertEqual(stable.config, candidate.config)
            invalid = candidate.replace(artifacts={**candidate.artifacts, "model": artifacts.put(
                b'{"model_id":"gpt-5.6-sol","reasoning_effort":"low"}', "model")})
            failed = agent.run(benchmark.task_view(task), invalid, context())
            self.assertEqual(failed.status, "error")
            self.assertEqual(failed.usage.model_calls, 0)

    def test_legacy_model_only_snapshots_keep_the_default_and_original_artifact_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            agent, snapshot, artifacts = fixture(Path(directory))
            self.assertNotIn("allowed_reasoning_efforts", snapshot.config)
            self.assertNotIn("initial_reasoning_effort", snapshot.config)
            self.assertEqual(artifacts.get(snapshot.artifacts["model"]), b'{\n  "model_id": "gpt-5.6-sol"\n}')
            args = agent._arguments(Path(directory), None, ModelSelection(model_id="gpt-5.6-sol"), "prompt", context())
            self.assertFalse(any(value.startswith("model_reasoning_effort=") for value in args))
            selected = ModelSelection(model_id="gpt-6-sol", reasoning_effort="high")
            args = agent._arguments(Path(directory), None, selected, "prompt", context())
            self.assertEqual(args[args.index("--model") + 1], selected.model_id)
            overrides = [args[index + 1] for index, value in enumerate(args[:-1]) if value == "--config"]
            self.assertEqual(tomllib.loads(next(value for value in overrides if value.startswith("model_reasoning_effort="))),
                {"model_reasoning_effort": "high"})

    def test_reasoning_catalog_and_initial_pair_are_validated_at_configuration_load(self):
        for change in ({"initial_reasoning_effort": "high"},
            {"allowed_reasoning_efforts": {"gpt-5.6-sol": []}},
            {"allowed_reasoning_efforts": {"gpt-5.6-sol": [None, None]}},
            {"allowed_reasoning_efforts": {"gpt-5.6-sol": ["high", "high"]}},
            {"allowed_models": ["gpt-5.6-sol"], "allowed_reasoning_efforts": {"gpt-6-sol": ["high"]}},
            {"allowed_reasoning_efforts": {"gpt-5.6-sol": ["unsupported"]}}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                CodexConfig(**change)

    def test_completed_turn_does_not_become_a_timeout_when_cli_shutdown_stalls(self):
        with tempfile.TemporaryDirectory() as directory:
            agent, snapshot, _ = fixture(Path(directory), mode="completed-hang", timeout_seconds=5.0)
            benchmark = ToyBenchmark()
            result = agent.run(benchmark.task_view(benchmark.tasks("dev")[0]), snapshot, context())
            self.assertEqual(result.status, "completed", result.error)
            self.assertTrue(result.state_summary["shutdown_after_terminal_event"])
            self.assertTrue(result.usage.usage_complete)
            self.assertEqual(result.usage.total_tokens, 25)
            self.assertLess(result.usage.elapsed_seconds, 5)

    def test_stdout_only_usage_limit_is_preserved_without_retrying_the_task(self):
        with tempfile.TemporaryDirectory() as directory:
            agent, snapshot, _ = fixture(Path(directory), mode="quota")
            benchmark = ToyBenchmark()
            result = agent.run(benchmark.task_view(benchmark.tasks("dev")[0]), snapshot, context())
            self.assertEqual(result.status, "error")
            self.assertIn("You have hit your usage limit", result.error.message)
            self.assertEqual(result.state_summary["startup_attempts"], 1)
            self.assertEqual(sum(event.kind == "model_request" for event in result.trajectory), 1)

    def test_completed_event_does_not_hide_a_natural_nonzero_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            agent, snapshot, _ = fixture(Path(directory), mode="completed-nonzero")
            benchmark = ToyBenchmark()
            result = agent.run(benchmark.task_view(benchmark.tasks("dev")[0]), snapshot, context())
            self.assertEqual(result.status, "error")
            self.assertFalse(result.state_summary["shutdown_after_terminal_event"])

    def test_runtime_facts_and_tool_read_rules_do_not_expose_benchmark_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent, snapshot, _ = fixture(root)
            benchmark = ToyBenchmark()
            result = agent.run(benchmark.task_view(benchmark.tasks("dev")[0]), snapshot, context())
            request = next(event for event in result.trajectory if event.kind == "model_request")
            public = json.loads(request.payload["request"]["messages"][-1]["content"])
            self.assertEqual(public["execution_environment"]["workspace"], result.state_summary["workspace"])
            self.assertNotIn("private_parameters", json.dumps(public))
            args = agent._arguments(root, None, "gpt-5.6-sol", "instructions", context())
            rules = [args[index + 1] for index, value in enumerate(args[:-1]) if value == "--config"]
            profile = tomllib.loads(next(value for value in rules if value.startswith("permissions.aer_task.filesystem=")))
            self.assertEqual(profile["permissions"]["aer_task"]["filesystem"][str(agent.project_root)], "deny")
            self.assertTrue(all(not path.is_relative_to(agent.project_root) for path in agent.tool_read_roots))

    @unittest.skipUnless(os.name == "nt", "MXC startup recovery")
    def test_transient_startup_failure_retries_but_started_threads_never_do(self):
        for mode, count, status in (("startup-once", 2, "completed"), ("startup-always", 3, "error"),
                                    ("startup-after-thread", 1, "error")):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                agent, snapshot, _ = fixture(root, mode=mode)
                benchmark = ToyBenchmark()
                result = agent.run(benchmark.task_view(benchmark.tasks("dev")[0]), snapshot, context())
                self.assertEqual(result.status, status)
                self.assertEqual(int((root / "startup-count.txt").read_text()), count)
                self.assertEqual(result.state_summary["startup_attempts"], count)
                self.assertEqual(sum(event.kind == "startup_retry" for event in result.trajectory), count - 1)
                if status == "completed":
                    self.assertEqual(result.usage.total_tokens, 25)

    def test_startup_retry_is_configurable_and_excludes_unknown_output_and_nonstartup_errors(self):
        failure = "failed to load AGENTS.md: CreateProcessSecurityEnvironment failed (HRESULT = 0x80070003)"
        self.assertTrue(retryable_startup_failure("", failure))
        for output in ('{"type":"thread.started"}', '{"type":"turn.started"}', '{"type":"item.completed"}',
                       'not-json', '[]'):
            self.assertFalse(retryable_startup_failure(output, failure))
        self.assertFalse(retryable_startup_failure("", "model request timed out"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent, snapshot, _ = fixture(root, mode="startup-always", startup_retries=0)
            benchmark = ToyBenchmark()
            result = agent.run(benchmark.task_view(benchmark.tasks("dev")[0]), snapshot, context())
            self.assertEqual(result.status, "error")
            self.assertEqual(int((root / "startup-count.txt").read_text()), 1)
    def test_snapshot_controls_model_output_scores_and_each_call_has_a_fresh_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent, stable, artifacts = fixture(root)
            benchmark = ToyBenchmark()
            task = benchmark.tasks("dev")[0]
            live = context(workspace_ref=str(root / "runs"))
            old = agent.run(benchmark.task_view(task), stable, live)
            ref = artifacts.put(ModelSelection(model_id="gpt-6-sol").model_dump_json().encode(), "model")
            candidate = stable.replace(version_id="v1", parent_version_id="v0", artifacts={**stable.artifacts, "model": ref})
            new = agent.run(benchmark.task_view(task), candidate, live)
            evaluator = DeterministicEvaluator()
            self.assertFalse(evaluator.evaluate_record(task, old, benchmark.evaluation_spec(task), operation_id="old-eval").success)
            self.assertTrue(evaluator.evaluate_record(task, new, benchmark.evaluation_spec(task), operation_id="new-eval").success)
            self.assertEqual(new.state_summary["execution_model"], "gpt-6-sol")
            self.assertNotEqual(old.state_summary["workspace"], new.state_summary["workspace"])
            self.assertEqual(stable.artifacts["prompt"], candidate.artifacts["prompt"])
            self.assertEqual(new.usage.total_tokens, 25)
            self.assertIsNone(new.usage.model_calls)
            self.assertTrue(new.usage.usage_complete)
            self.assertEqual(new.usage.tool_calls, 1)
            self.assertIsNone(sum_usage([new.usage, old.usage]).model_calls)
            self.assertEqual([event.kind for event in new.trajectory].count("tool_call"), 1)
            model_request = new.trajectory[0].model_dump(mode="json")
            self.assertNotIn("private_parameters", json.dumps(model_request))
            self.assertEqual(agent.snapshot(), stable)
            agent.restore(candidate)
            self.assertEqual(agent.snapshot(), candidate)

    def test_invalid_output_and_missing_usage_are_not_silently_repaired(self):
        for mode in ("bad-json", "missing-usage", "failure"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                agent, snapshot, _ = fixture(Path(directory), mode=mode)
                benchmark = ToyBenchmark()
                result = agent.run(benchmark.task_view(benchmark.tasks("dev")[0]), snapshot, context())
                if mode == "bad-json":
                    self.assertEqual(result.output, {})
                    self.assertEqual(result.state_summary["termination_reason"], "invalid_output_format")
                    self.assertEqual(result.status, "completed")
                elif mode == "missing-usage":
                    self.assertFalse(result.usage.usage_complete)
                    self.assertIsNone(result.usage.total_tokens)
                else:
                    self.assertEqual(result.status, "error")
                    self.assertIsNotNone(result.error)

    def test_timeout_terminates_the_process_and_returns_an_associated_error(self):
        with tempfile.TemporaryDirectory() as directory:
            agent, snapshot, _ = fixture(Path(directory), mode="timeout", timeout_seconds=0.2)
            benchmark = ToyBenchmark()
            result = agent.run(benchmark.task_view(benchmark.tasks("dev")[0]), snapshot, context())
            self.assertEqual(result.status, "error")
            self.assertIn("timeout", result.error.message)
            self.assertEqual(result.operation_id, context().operation_id)

    def test_timeout_preserves_network_errors_and_distinguishes_a_started_model_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            agent, snapshot, _ = fixture(Path(directory), mode="timeout-after-thread", timeout_seconds=0.3)
            benchmark = ToyBenchmark()
            result = agent.run(benchmark.task_view(benchmark.tasks("dev")[0]), snapshot, context())
            self.assertEqual(result.status, "error")
            self.assertTrue(result.state_summary["session_started"])
            self.assertTrue(result.state_summary["turn_started"])
            self.assertIn("timeout during model turn", result.error.message)
            self.assertIn("fixture model connection timed out", result.error.message)

    def test_a_recovered_runtime_notice_does_not_override_a_completed_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            agent, snapshot, _ = fixture(Path(directory), mode="recoverable-error")
            benchmark = ToyBenchmark()
            result = agent.run(benchmark.task_view(benchmark.tasks("dev")[0]), snapshot, context())
            self.assertEqual(result.status, "completed")
            self.assertTrue(result.usage.usage_complete)
            self.assertTrue(any(event.kind == "runtime_notice" for event in result.trajectory))

    def test_nonzero_command_exit_is_visible_as_a_tool_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            agent, snapshot, _ = fixture(Path(directory), mode="tool-error")
            benchmark = ToyBenchmark()
            result = agent.run(benchmark.task_view(benchmark.tasks("dev")[0]), snapshot, context())
            tool = next(event for event in result.trajectory if event.kind == "tool_result")
            self.assertEqual(tool.payload["status"], "error")
            self.assertEqual(tool.payload["error"]["code"], "tool_error")

    def test_cli_arguments_use_snapshot_model_public_schema_and_restrict_grading_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            agent, _, _ = fixture(Path(directory))
            root = Path(directory)
            args = agent._arguments(root, root / "output-schema.json", "gpt-6-luna", "line 1\n中文", context())
            self.assertEqual(args[args.index("--model") + 1], "gpt-6-luna")
            self.assertIn("--ephemeral", args)
            self.assertNotIn("resume", args)
            overrides = [args[index + 1] for index, value in enumerate(args[:-1]) if value == "--config"]
            self.assertTrue(any(value.startswith("permissions.aer_task.filesystem=") and "deny" in value for value in overrides))
            self.assertTrue(any(value.startswith("developer_instructions=") for value in overrides))
            self.assertIn("--ignore-user-config", args)
            if os.name == "nt":
                self.assertIn('windows.sandbox="mxc"', overrides)
                self.assertNotIn('windows.sandbox="unelevated"', overrides)
            for value in overrides:
                tomllib.loads(value)
            profile = tomllib.loads(next(value for value in overrides if value.startswith("permissions.aer_task.filesystem=")))
            self.assertEqual(profile["permissions"]["aer_task"]["filesystem"][":root"], "deny")

    def test_logical_run_workspace_inside_project_uses_its_existing_parent_denial(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent, _, _ = fixture(root)
            logical = agent.project_root / "runs" / root.name / "workspaces" / "episode" / "initial_execution"
            self.assertFalse(logical.exists())
            args = agent._arguments(root, None, "gpt-5.6-sol", "instructions",
                                    context(workspace_ref=str(logical)))
            overrides = [args[index + 1] for index, value in enumerate(args[:-1]) if value == "--config"]
            profile = tomllib.loads(next(value for value in overrides if value.startswith("permissions.aer_task.filesystem=")))
            filesystem = profile["permissions"]["aer_task"]["filesystem"]
            self.assertEqual(filesystem[str(agent.project_root)], "deny")
            self.assertNotIn(str(logical), filesystem)
            self.assertFalse(logical.exists())
            # An external run directory must still have its explicit denial.
            external = root / "external-run"
            external.mkdir()
            args = agent._arguments(root, None, "gpt-5.6-sol", "instructions",
                                    context(workspace_ref=str(external)))
            rules = [args[index + 1] for index, value in enumerate(args[:-1]) if value == "--config"]
            profile = tomllib.loads(next(value for value in rules if value.startswith("permissions.aer_task.filesystem=")))
            self.assertEqual(profile["permissions"]["aer_task"]["filesystem"][str(external)], "deny")

    @unittest.skipUnless(os.name == "nt", "Windows-specific backend")
    def test_windows_backend_is_explicit_configurable_and_part_of_runtime_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent, snapshot, _ = fixture(root, windows_sandbox="elevated")
            args = agent._arguments(root, None, "gpt-5.6-sol", "instructions", context())
            self.assertIn('windows.sandbox="elevated"', args)
            self.assertEqual(snapshot.config["windows_sandbox"], "elevated")
            self.assertEqual(agent.runtime_info()["windows_sandbox"], "elevated")
            with self.assertRaises(ValueError):
                CodexConfig(windows_sandbox="unelevated")

    def test_explicit_public_schema_is_supported_without_benchmark_type_dependencies(self):
        schema = {"type": "object", "properties": {"value": {"type": "boolean"}},
                  "required": ["value"], "additionalProperties": False}
        task = TaskView(task_id="generic", benchmark_id="other_benchmark", input={"output_schema": schema})
        self.assertEqual(PublicJsonOutput().schema(task), schema)
        self.assertEqual(PublicJsonOutput().parse('{"value":true}'), {"value": True})

    def test_legacy_host_permissions_cannot_silently_disable_task_isolation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config.toml").write_text('sandbox_mode="danger-full-access"\n', encoding="utf-8")
            agent, snapshot, _ = fixture(root, ignore_user_config=False)
            benchmark = ToyBenchmark()
            with patch.dict("os.environ", {"CODEX_HOME": str(root)}):
                result = agent.run(benchmark.task_view(benchmark.tasks("dev")[0]), snapshot, context())
            self.assertEqual(result.status, "error")
            self.assertIn("legacy sandbox settings", result.error.message)
            self.assertEqual(result.usage.total_tokens, 0)

