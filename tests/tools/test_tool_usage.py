import json
import unittest
import asyncio
import tempfile
import tomllib
from pathlib import Path

from aer_core.tool_usage import tool_usage_summary
from experiments.tool_usage import analyze_tool_usage
from plugins.tools.generated.contracts import classify_result
from plugins.tools.invocation_receipts import begin, finish
from tests.tools.test_generated import tool
from tests.evolution.test_oscopilot import node
from tests.tools.test_generated import HAS_KERNEL


def instrumented_tool():
    interface = node("Inspect source", deliverable=True)["interface"]
    return tool(usage=interface["usage"], usage_example={"path": "."}, output_schema=interface["output_schema"],
        runtime_outcome=interface["runtime_outcome"])


def report():
    return {"invocation_count": 0, "invocations": []}


class ToolUsageTests(unittest.TestCase):
    def test_zero_invocations_have_no_success_rate_or_invented_nonuse_reason(self):
        value = tool_usage_summary({"available_tools": ["inspect"], "tool_metrics": {}, "tool_discovered": True})
        item = value["tools"]["inspect"]
        self.assertFalse(item["invoked"])
        self.assertIsNone(item["execution_success_rate"])
        self.assertIsNone(item["business_success_rate"])
        self.assertIsNone(item["non_use_reason"])
        self.assertEqual(item["result_use_status"], "not_assessed")

    def test_started_receipt_survives_serialization_and_is_not_counted_as_completed(self):
        value = report()
        begin(value, "inspect", "generated", {"path": "."})
        saved = json.loads(json.dumps(value))
        summary = tool_usage_summary({"available_tools": ["inspect"], **saved})
        self.assertEqual(summary["tools"]["inspect"]["pending"], 1)
        self.assertEqual(saved["invocations"][0]["status"], "started")
        self.assertIsNone(summary["tools"]["inspect"]["execution_success_rate"])

    def test_totals_include_all_calls_and_completion_of_evicted_pending_receipt(self):
        value = report()
        first = begin(value, "inspect", "generated", {})
        for _ in range(105):
            entry = begin(value, "inspect", "generated", {})
            finish(value, entry, error=False, elapsed=0.5, outcome={"business_status": "success"})
        finish(value, first, error=True, elapsed=2.0)
        self.assertEqual(len(value["invocations"]), 100)
        self.assertEqual(value["invocations_omitted"], 6)
        self.assertEqual(value["tool_metrics"]["inspect"]["attempted"], 106)
        self.assertEqual(value["tool_metrics"]["inspect"]["completed"], 106)
        self.assertEqual(value["tool_metrics"]["inspect"]["business_success"], 105)
        self.assertEqual(value["tool_metrics"]["inspect"]["execution_error"], 1)

    def test_general_analysis_can_succeed_on_no_findings_and_fail_on_reported_error(self):
        generated = instrumented_tool()
        success = classify_result(generated, {"status": "completed", "error": None, "result": {"exists": False}})
        self.assertEqual(success["business_status"], "success")
        failed = classify_result(generated, {"status": "completed", "error": None, "result": {"error": "File not found"}})
        self.assertEqual(failed["business_status"], "failure")
        invalid = classify_result(generated, {"status": "completed", "error": None, "result": []})
        self.assertFalse(invalid["output_valid"])
        self.assertEqual(invalid["business_status"], "failure")
        runtime = classify_result(generated, {"status": "error", "error": "timeout", "result": None})
        self.assertEqual(runtime["business_status"], "not_evaluated")

    def test_legacy_ambiguous_and_unclassified_results_are_unknown(self):
        output = {"status": "completed", "error": None, "result": {"exists": True, "error": "ambiguous"}}
        self.assertEqual(classify_result(tool(), output)["business_status"], "unknown")
        self.assertEqual(classify_result(instrumented_tool(), output)["outcome_reason"], "ambiguous_outcome")
        output["result"] = {}
        self.assertEqual(classify_result(instrumented_tool(), output)["outcome_reason"], "unclassified_outcome")

    def test_descriptor_publishes_guidance_and_valid_example_without_forcing_use(self):
        generated = instrumented_tool()
        description = generated.descriptor().description
        for key in ("capability_gap", "use_when", "added_value", "result_usage", "preconditions", "cost"):
            self.assertIn(key + ":", description)
        self.assertIn('"path": "."', description)
        with self.assertRaises(Exception):
            generated.replace(usage_example={"querysets": []})

    def test_remote_request_payloads_are_not_copied_to_receipts(self):
        value = report()
        entry = begin(value, "remote", "mcp", {"secret": "PRIVATE_TOKEN"})
        finish(value, entry, error=False, elapsed=0.2, result={"secret": "PRIVATE_TOKEN"})
        self.assertNotIn("PRIVATE_TOKEN", json.dumps(value))
        self.assertNotIn("arguments_digest", entry)

    def test_group_analysis_keeps_costs_caching_and_noncausal_task_deltas(self):
        def scored(calls, score, tokens):
            value = report()
            for _ in range(calls):
                entry = begin(value, "inspect", "generated", {})
                finish(value, entry, error=False, elapsed=0.1, outcome={"business_status": "success"})
            usage = tool_usage_summary({"available_tools": ["inspect"], "tool_metrics": {}, **value})
            return {"execution": {"tool_usage": usage, "usage": {"total_tokens": tokens, "elapsed_seconds": 2}},
                "evaluation": {"score": score}}
        pairs = [{"group": group, "task": group + "-task", "baseline_cached": True,
            "baseline": scored(0, 0, 100), "candidate": scored(1 if group == "local" else 0, 1, 120)}
            for group in ("local", "similar", "regression")]
        analysis = analyze_tool_usage(pairs)
        self.assertEqual(set(analysis["groups"]), {"local", "similar", "regression"})
        self.assertEqual(analysis["groups"]["local"]["candidate"]["inspect"]["business_success_rate"], 1)
        self.assertIsNone(analysis["groups"]["similar"]["candidate"]["inspect"]["business_success_rate"])
        self.assertEqual(analysis["groups"]["similar"]["tasks"][0]["score_delta"], 1)
        self.assertEqual(analysis["groups"]["local"]["tasks"][0]["token_delta"], 20)
        self.assertTrue(analysis["groups"]["local"]["tasks"][0]["baseline"]["cached"])
        self.assertIn("not causal", analysis["attribution"])

    def test_legacy_call_tail_does_not_claim_complete_totals(self):
        summary = tool_usage_summary({"available_tools": ["inspect"], "tool_invocations": [
            {"tool": "inspect", "status": "completed", "elapsed_seconds": 1.0}]})
        self.assertFalse(summary["counts_complete"])
        self.assertIsNone(summary["tools"]["inspect"]["business_success_rate"])


@unittest.skipUnless(HAS_KERNEL, "install ipykernel")
class ToolUsageMCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_bridge_persists_started_call_and_distinguishes_business_failure(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        from plugins.agents.tool_selection import CodexToolAdapter
        from plugins.tools.generated.bundle import ToolBundle
        from plugins.tools.catalog import ToolingConfig
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            good = instrumented_tool().replace(source="def source_summary(path, workspace):\n    import time\n    time.sleep(0.2)\n    return {'exists': False}")
            bad = instrumented_tool().replace(source="def source_summary(path, workspace):\n    return {'error': 'Missing dependency'}")
            bundle = ToolBundle(enabled_tools=["good", "bad"], generated_tools={"good": good, "bad": bad})
            active = CodexToolAdapter().prepare(bundle, ToolingConfig(catalog={}, generated_tools={"enabled": True}), root, Path.cwd())
            config = tomllib.loads(active.overrides[0])["mcp_servers"]["aer_tools"]
            try:
                async with stdio_client(StdioServerParameters(command=config["command"], args=config["args"])) as streams:
                    async with ClientSession(*streams) as session:
                        await session.initialize()
                        definitions = (await session.list_tools()).tools
                        self.assertIn("use_when", definitions[0].description)
                        pending = asyncio.create_task(session.call_tool("good", {"path": "."}))
                        for _ in range(100):
                            if active.report()["invocations"]:
                                break
                            await asyncio.sleep(0.05)
                        self.assertEqual(active.report()["invocations"][0]["status"], "started")
                        result = await pending
                        self.assertFalse(result.isError)
                        result = await session.call_tool("bad", {"path": "."})
                        self.assertFalse(result.isError)  # Runtime completed; declared business outcome failed.
                        receipt = active.report()
                        self.assertEqual(receipt["tool_metrics"]["good"]["business_success"], 1)
                        self.assertEqual(receipt["tool_metrics"]["bad"]["execution_success"], 1)
                        self.assertEqual(receipt["tool_metrics"]["bad"]["business_failure"], 1)
                        self.assertEqual(receipt["invocations"][0]["business_status"], "success")
                        self.assertEqual(receipt["invocations"][1]["business_status"], "failure")
                        self.assertTrue(receipt["invocations"][1]["implementation_digest"])
            finally:
                active.close()
