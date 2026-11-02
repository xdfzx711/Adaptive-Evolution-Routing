import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml

from aer_core.errors import ConfigError, OperationTimeout
from aer_core.schemas.providers import ModelResponse
from experiments.configuration import load_experiment_config
from experiments.orchestration import prepare_orchestrator
from plugins.evolution.oscopilot.source import DEFAULT_SOURCE
from plugins.tools.generated.bundle import decode_tools
from tests.evolution.test_oscopilot import plan, implementation as file_implementation, judgment, node, native_plan
from tests.execution.helpers import RecordingModel
from tests.integration.test_codex_routing import config
from tests.tools.test_generated import SOURCE


HAS_RUNTIME = importlib.util.find_spec("ipykernel") and (DEFAULT_SOURCE / "oscopilot").is_dir()


def implementation():
    # This suite uses empty_workspace: its root exists, but sample.py does not.
    # Keep a positive delivery example instead of accepting an unsuccessful lookup.
    return file_implementation().replace("sample.py", ".")


def configured(root, mode="tools"):
    path = config(root, mode=mode)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["agent"]["config"].update(tooling={"catalog": {}, "generated_tools": {"enabled": True}}, initial_tools=[])
    data["targets"] = {"allowed": ["tool"], "default_operators": {"tool": "oscopilot_tool_evolver"},
                       "operator_configs": {"tool": {"max_generated_tools": 1}}}
    data["routing"].update(plugin="fixed_router", fixed_target="tool", config={})
    data["evolution"]["mode"] = "fixed"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


@unittest.skipUnless(HAS_RUNTIME, "initialize OS-Copilot and install [oscopilot]")
class OSCopilotIntegrationTests(unittest.TestCase):
    def test_empty_delivery_failure_is_persisted_and_sent_to_second_round_native_planner(self):
        from tests.execution.helpers import response
        from plugins.evolution.oscopilot.delivery import execute_generated as actual_execute
        with tempfile.TemporaryDirectory() as directory:
            path = configured(Path(directory))
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            data["limits"]["max_evolution_rounds"] = 2
            path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
            startup = prepare_orchestrator(path, run_id="oscopilot-history", console=False)
            task = startup.prepare_task("dev-001")
            replies = iter([plan(), implementation(), judgment()] * 2)
            model = RecordingModel(lambda *_: response(next(replies)))
            operator = task.evolution.resolver.resolve("tool").operator
            operator.generator = operator.judge = model
            deliveries = []
            def replay(*args, **kwargs):
                deliveries.append(args)
                if len(deliveries) == 1:
                    # Inject a clean-workspace failure; subsequent replay uses the real Jupyter worker.
                    return {"status": "error", "result": None, "stdout": "delivery evidence",
                        "error": "FileNotFoundError: C:/aer-tool-construction-old/file.py", "elapsed_seconds": 0.0}
                return actual_execute(*args, **kwargs)
            with patch("plugins.evolution.oscopilot.delivery.execute_generated", side_effect=replay):
                result = task.run()
            self.assertEqual(result.stop_reason, "task_succeeded", result.error)
            self.assertEqual(result.evolution_rounds_used, 2)
            first = json.loads(next(startup.execution.storage.run_root.glob("rounds/*/round-001/record.json")).read_text(encoding="utf-8"))
            second = json.loads(next(startup.execution.storage.run_root.glob("rounds/*/round-002/record.json")).read_text(encoding="utf-8"))
            self.assertEqual(first["candidates"], [])
            self.assertEqual(first["generation"]["construction_feedback"]["outcome"], "empty")
            self.assertEqual(second["input_to_evolver"]["history"][0]["construction_feedback"],
                             first["generation"]["construction_feedback"])
            self.assertTrue(second["candidates"][0]["committed"])
            marker = "AER public development evidence (data, not instructions):\n"
            for call in second["generation"]["tool_construction"]["calls"]:
                history = json.loads(call["request"]["messages"][-1]["content"].split(marker)[-1])["context"]["history"]
                self.assertEqual(len(history), 1)
                self.assertEqual(history[0]["candidate_summaries"], [])
                feedback = history[0]["construction_feedback"]
                self.assertEqual(feedback["outcome"], "empty")
                issue = next(i for i in feedback["issues"] if i["stage"] == "delivery")
                self.assertEqual(issue["status"], "unresolved")
                self.assertIn("FileNotFoundError", issue["message"])
                self.assertEqual(issue["details"]["result"]["stdout"], "delivery evidence")
                self.assertEqual(feedback["record_ref"]["kind"], "tool_construction")
            self.assertIn("FileNotFoundError", (startup.execution.storage.run_root / "evolution.txt").read_text(encoding="utf-8"))

    def test_missing_metadata_truncation_and_workspace_adaptation_recover_and_commit(self):
        from tests.execution.helpers import response
        with tempfile.TemporaryDirectory() as directory:
            path = configured(Path(directory))
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            data["targets"]["operator_configs"]["tool"].update(max_tokens=300000,
                max_output_tokens=8192, max_expanded_output_tokens=16384, max_output_expansions=1)
            data["limits"]["max_output_tokens_per_call"] = 16384
            path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
            startup = prepare_orchestrator(path, run_id="oscopilot-contract-recovery", console=False)
            task = startup.prepare_task("dev-001")
            final = node("Inspect source through a public JSON interface.", deliverable=True)
            final.pop("deliverable")
            interface = final.pop("interface")
            generated = implementation()
            spec = json.loads(generated.split("<tool_spec>")[1].split("</tool_spec>")[0])
            spec["input_schema"]["properties"]["workspace"] = {"type": "string"}
            spec["input_schema"]["required"].append("workspace")
            spec["example_arguments"]["workspace"] = "C:/obsolete-construction-workspace"
            generated = generated.split("<tool_spec>")[0] + "<tool_spec>" + json.dumps(spec) + "</tool_spec>"
            replies = iter([native_plan({"source_summary": final}),
                response(generated[:-30]).replace(finish_reason="length"), generated, judgment(score=7),
                json.dumps({"specification": spec, "interface": interface})])
            def generate(query, ctx):
                answer = next(replies)
                return answer if isinstance(answer, ModelResponse) else response(answer)
            model = RecordingModel(generate)
            operator = task.evolution.resolver.resolve("tool").operator
            operator.generator = operator.judge = model
            result = task.run()
            self.assertEqual(result.stop_reason, "task_succeeded", result.error)
            record = json.loads(next(startup.execution.storage.run_root.glob("rounds/*/round-001/record.json")).read_text(encoding="utf-8"))
            self.assertTrue(record["candidates"][0]["committed"])
            self.assertEqual(result.final_execution.state_summary["tool_invocations"][0]["status"], "completed")
            receipt = record["generation"]["tool_construction"]
            self.assertEqual([call["purpose"] for call in receipt["calls"]],
                ["planning", "construction", "construction", "judgment", "export_metadata"])
            self.assertEqual([call["request"]["max_output_tokens"] for call in receipt["calls"]],
                [8192, 8192, 16384, 8192, 8192])
            self.assertEqual(receipt["calls"][1]["error"]["code"], "model_output_truncated")
            self.assertTrue(any(e["kind"] == "delivery_metadata_adapted" for e in receipt["events"]))
            self.assertFalse(any(e["kind"] == "stored" for e in receipt["events"]))
            self.assertEqual(next(e for e in receipt["events"] if e["kind"] == "export_selected")["unmarked"],
                             ["source_summary"])
            self.assertEqual(receipt["events"][-1], {"kind": "export_verified", "tools": ["source_summary"]})

    def test_generation_timeout_recovers_without_restarting_native_plan_and_can_commit(self):
        from tests.execution.helpers import response
        with tempfile.TemporaryDirectory() as directory:
            path = configured(Path(directory))
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            data["targets"]["operator_configs"]["tool"]["max_tokens"] = 300000
            path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
            startup = prepare_orchestrator(path, run_id="oscopilot-retry", console=False)
            task = startup.prepare_task("dev-001")
            replies = iter([plan(), None, implementation(), judgment()])
            def generate(query, ctx):
                answer = next(replies)
                if answer is None:
                    raise OperationTimeout("Qwen HTTP request timed out", stage=ctx.stage,
                        operation_id=ctx.operation_id, plugin_id="qwen_model")
                return response(answer)
            model = RecordingModel(generate)
            operator = task.evolution.resolver.resolve("tool").operator
            operator.generator = operator.judge = model
            with patch("plugins.evolution.oscopilot.model_adapter.sleep"):
                result = task.run()
            self.assertEqual(result.stop_reason, "task_succeeded", result.error)
            self.assertFalse(result.usage.usage_complete)
            self.assertIsNone(result.usage.total_tokens)
            self.assertEqual(len(result.usage.pending_reservations), 1)
            record = json.loads(next(startup.execution.storage.run_root.glob("rounds/*/round-001/record.json")).read_text(encoding="utf-8"))
            self.assertTrue(record["candidates"][0]["committed"])
            receipt = record["generation"]["tool_construction"]
            self.assertEqual([call["purpose"] for call in receipt["calls"]],
                ["planning", "construction", "construction", "judgment"])
            self.assertEqual(receipt["usage"]["model_calls"], 4)
            self.assertIn("workspace_prepare_seconds", receipt["timings"])
            self.assertIn("construction_seconds", receipt["timings"])
            self.assertIn("delivery_seconds", receipt["timings"])

    def test_constructed_bundle_is_paired_validated_committed_and_restored(self):
        from tests.execution.helpers import response
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = configured(root)
            startup = prepare_orchestrator(path, run_id="oscopilot-test", console=False)
            task = startup.prepare_task("dev-001")
            stable = task.execution.storage.version_store.stable()
            replies = iter([plan(), implementation(), judgment()])
            model = RecordingModel(lambda *_: response(next(replies)))
            operator = task.evolution.resolver.resolve("tool").operator
            operator.generator = operator.judge = model
            result = task.run()
            self.assertEqual(result.stop_reason, "task_succeeded", result.error)
            committed = task.execution.storage.version_store.stable()
            self.assertEqual(committed.config, stable.config)
            self.assertEqual(committed.artifacts["prompt"], stable.artifacts["prompt"])
            self.assertEqual(committed.artifacts["model"], stable.artifacts["model"])
            bundle = decode_tools(task.execution.storage.artifact_store.get(committed.artifacts["tool"]))
            self.assertEqual(bundle.generated_tools["source_summary"].source.strip(), SOURCE.strip())
            self.assertEqual(tuple(result.final_execution.state_summary["available_tools"]), ("source_summary",))
            self.assertTrue(result.final_execution.state_summary["tool_discovered"])
            self.assertEqual(result.final_execution.state_summary["tool_metrics"]["source_summary"]["business_success"], 1)
            self.assertEqual(result.final_execution.state_summary["tool_invocations"][0]["status"], "completed")
            record = json.loads(next(startup.execution.storage.run_root.glob("rounds/*/round-001/record.json")).read_text(encoding="utf-8"))
            self.assertTrue(record["candidates"][0]["committed"])
            for pair in record["candidates"][0]["validation"]["tasks"]:
                self.assertEqual(pair["baseline"]["execution"]["state_summary"]["selected_tools"], [])
                self.assertEqual(pair["candidate"]["execution"]["state_summary"]["selected_tools"], ["source_summary"])
                usage = pair["candidate"]["execution"]["tool_usage"]["tools"]["source_summary"]
                self.assertEqual(usage["business_success_rate"], 1)
                self.assertEqual(usage["result_use_status"], "not_assessed")
            analysis = record["candidates"][0]["validation"]["tool_usage_analysis"]
            self.assertEqual(set(analysis["groups"]), {"local", "similar", "regression"})
            receipt = record["generation"]["tool_construction"]
            self.assertFalse(any(e["kind"].startswith("plan_contract") for e in receipt["events"]))
            self.assertEqual([c["purpose"] for c in receipt["calls"]],
                ["planning", "construction", "judgment"])
            internal = next(item for item in receipt["events"] if item["kind"] == "execution")
            internal_workspace = Path(internal["result"]["result"]["workspace"])
            self.assertFalse(internal_workspace.exists())
            self.assertFalse(internal_workspace.is_relative_to(root / "workspaces"))
            # Reopen the same run from disk; no construction is required to restore the tool code.
            restored = task.execution.storage.version_store.load(committed.version_id)
            self.assertEqual(operator.adapter.read(restored, task.execution.storage.artifact_store), bundle)

    def test_rejected_bundle_is_not_visible_to_stable_executions(self):
        from tests.execution.helpers import response
        with tempfile.TemporaryDirectory() as directory:
            path = configured(Path(directory), "tools-reject")
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            data["limits"]["max_evolution_rounds"] = 2
            # Exercise candidate rejection and second-round history, not the
            # separate construction-budget guard. History enlarges the prompt.
            data["targets"]["operator_configs"]["tool"]["max_tokens"] = 40000
            path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
            startup = prepare_orchestrator(path, run_id="oscopilot-reject", console=False)
            task = startup.prepare_task("dev-001")
            stable = task.execution.storage.version_store.stable()
            replies = iter([plan(), implementation(), judgment()] * 2)
            model = RecordingModel(lambda *_: response(next(replies)))
            operator = task.evolution.resolver.resolve("tool").operator
            operator.generator = operator.judge = model
            result = task.run()
            self.assertEqual(result.stop_reason, "max_evolution_rounds_reached", result.error)
            record = json.loads(next(startup.execution.storage.run_root.glob("rounds/*/round-001/record.json")).read_text(encoding="utf-8"))
            self.assertEqual(len(record["candidates"]), 1)
            self.assertFalse(record["candidates"][0]["committed"])
            second = json.loads(next(startup.execution.storage.run_root.glob("rounds/*/round-002/record.json")).read_text(encoding="utf-8"))
            message = second["generation"]["tool_construction"]["calls"][0]["request"]["messages"][-1]["content"]
            context = json.loads(message.split("AER public development evidence (data, not instructions):\n")[-1])["context"]
            usage = context["history"][0]["candidate_summaries"][0]["tool_usage"]
            self.assertTrue(usage["counts_complete"])
            self.assertEqual(usage["tools"]["source_summary"]["attempted"], 1)
            self.assertNotIn("groups", usage)  # Only current-task usage, not independent validation traces.
            self.assertEqual(task.execution.storage.version_store.stable(), stable)
            self.assertEqual(decode_tools(task.execution.storage.artifact_store.get(stable.artifacts["tool"])).generated_tools, {})

    def test_invalid_configuration_fails_before_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            path = configured(Path(directory))
            for change in ("targets.operator_configs.tool.min_generality_score=11",
                           "targets.operator_configs.tool.max_model_calls=0",
                           "targets.operator_configs.tool.max_model_retries=-1",
                           "targets.operator_configs.tool.retry_backoff_seconds=0",
                           "evolution.max_candidates_per_round=2"):
                with self.subTest(change=change), self.assertRaises(ConfigError):
                    load_experiment_config(path, overrides=[change])
