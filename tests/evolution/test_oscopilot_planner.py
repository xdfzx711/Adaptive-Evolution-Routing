"""Graph and context regressions using native methods and a controlled execution result."""

import json
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
import unittest
from unittest.mock import patch

from plugins.evolution.oscopilot.config import OSCopilotConfig
from plugins.evolution.oscopilot.evolver import OSCopilotToolEvolver
from plugins.evolution.oscopilot.source import DEFAULT_SOURCE, load_upstream
from plugins.tools.generated.bundle import ToolBundle, decode_tools
from tests.tools.test_generated import tool
from tests.evolution.test_oscopilot import Workspaces, request_fixture, native_plan, node, implementation, judgment
from tests.execution.helpers import RecordingModel, response


@unittest.skipUnless((DEFAULT_SOURCE / "oscopilot").is_dir(), "initialize the OS-Copilot submodule")
class FridayPlannerTests(unittest.TestCase):
    def test_delivery_guidance_reaches_native_planning_generation_and_repair_only(self):
        import copy
        from plugins.evolution.oscopilot.delivery_guidance import DELIVERY_GUIDANCE
        upstream = load_upstream(None)
        original = copy.deepcopy(upstream.prompts)
        result, receipt, _, _, worker = self.run_graph([
            native_plan({'source_summary': node('Inspect', deliverable=True)}),
            implementation(), judgment('Amend'), implementation(), judgment()])
        self.assertEqual(result.status, 'completed', result.error)
        self.assertEqual(worker.call_count, 2)
        self.assertEqual([c['purpose'] for c in receipt['calls']],
                         ['planning', 'construction', 'judgment', 'repair', 'judgment'])
        for call in receipt['calls']:
            system = '\n'.join(m['content'] for m in call['request']['messages'] if m['role'] == 'system')
            if call['purpose'] in {'planning', 'construction', 'repair'}:
                self.assertIn(DELIVERY_GUIDANCE, system)
            else:
                self.assertNotIn(DELIVERY_GUIDANCE, system)
        self.assertEqual(upstream.prompts, original)

    def test_large_execution_is_kept_in_receipt_but_bounded_for_successor(self):
        graph = {'source_summary': node('Inspect', deliverable=True),
                 'analyze': node('Analyze the output', ['source_summary'], 'QA')}
        value = {'exists': True, 'full_content': 'source line\n' * 40000}
        result, receipt, _, _, _ = self.run_graph([
            native_plan(graph), implementation(), judgment(), 'analysis complete'],
            execution_value=value, delivery_value={'exists': True})
        self.assertEqual(result.status, 'completed', result.error)
        index, execution = next((i, e) for i, e in enumerate(receipt['events']) if e['kind'] == 'execution')
        self.assertEqual(execution['result']['result'], value)
        completed = next(e for e in receipt['events'] if e['kind'] == 'node_completed')
        view = json.loads(completed['return_val'])
        self.assertTrue(view['result']['truncated'])
        self.assertEqual(view['full_output_ref']['event_index'], index)
        self.assertIn('trial-stdout', view['stdout'])
        successor = next(c for c in receipt['calls'] if c['purpose'] == 'analysis')
        self.assertLess(sum(len(m['content']) for m in successor['request']['messages']), 20000)

    def test_completed_helper_does_not_activate_export_reserve_or_block_final_judgment(self):
        graph = {'helper': node('Internal helper'),
                 'source_summary': node('Final capability', ['helper'], deliverable=True)}
        result, receipt, _, _, worker = self.run_graph([
            native_plan(graph), implementation(), judgment(), implementation(), judgment()],
            max_tokens=20000, export_token_reserve=19000)
        self.assertEqual(result.status, 'completed', result.error)
        self.assertEqual(worker.call_count, 2)
        self.assertTrue(all(c['export_reserved_tokens'] == 0 for c in receipt['calls']))
        self.assertTrue(any(c['judgment_reserved_tokens'] > 0 for c in receipt['calls']))

    def test_insufficient_repair_pair_budget_stops_before_repair_request(self):
        from plugins.evolution.oscopilot.model_adapter import ModelAdapter
        def estimate(adapter, messages):
            return 400 if adapter.purpose == 'repair' else 100
        with patch.object(ModelAdapter, 'estimate_input_tokens', estimate):
            result, receipt, _, _, worker = self.run_graph([
                native_plan({'source_summary': node('Final', deliverable=True)}), implementation(), judgment('Amend')],
                max_tokens=1000, max_output_tokens=100)
        self.assertEqual(result.status, 'budget_interrupted')
        self.assertIn('request and judgment', result.error.message)
        worker.assert_called_once()
        self.assertFalse(result.candidates)
        self.assertEqual([c['purpose'] for c in receipt['calls']], ['planning', 'construction', 'judgment'])

    def test_format_failure_is_empty_with_public_feedback_instead_of_fatal_error(self):
        result, receipt, _, _, worker = self.run_graph([
            native_plan({"source_summary": node("Inspect", deliverable=True)}),
            '```python\ndef source_summary(path, workspace): return {}\n```'])
        self.assertEqual(result.status, "empty", result.error)
        self.assertIsNone(result.error)
        worker.assert_not_called()
        issue = next(i for i in result.construction_feedback.issues if i.code == "response_format_failed")
        self.assertIn('def source_summary', issue.details['response'])

    def test_format_recovery_is_separate_from_native_repair_and_keeps_original_metadata(self):
        original = implementation() + '\n<invoke>source_summary("different.py", workspace=workspace)</invoke>'
        result, receipt, artifacts, _, worker = self.run_graph([
            native_plan({"source_summary": node("Inspect", deliverable=True)}), original,
            '{"code_candidate": 0, "invoke_candidate": 0}', judgment()])
        self.assertEqual(result.status, "completed", result.error)
        self.assertEqual([c['purpose'] for c in receipt['calls']],
                         ['planning', 'construction', 'response_format', 'judgment'])
        worker.assert_called_once()
        self.assertEqual(set(decode_tools(artifacts.get(result.candidates[0].artifact_updates['tool'])).generated_tools),
                         {'source_summary'})
        self.assertNotIn('AER public development evidence', receipt['calls'][2]['request']['messages'][-1]['content'])

    def test_unrecoverable_repair_format_returns_empty_with_feedback(self):
        result, receipt, _, _, worker = self.run_graph([
            native_plan({"source_summary": node("Inspect", deliverable=True)}), implementation(), judgment('Amend'),
            'No executable response'])
        self.assertEqual(result.status, 'empty', result.error)
        worker.assert_called_once()
        self.assertTrue(any(i.code == 'response_format_failed' for i in result.construction_feedback.issues))
        self.assertEqual([c['purpose'] for c in receipt['calls']], ['planning', 'construction', 'judgment', 'repair'])

    def test_export_reserve_can_publish_completed_tools_without_starting_next_node(self):
        from plugins.evolution.oscopilot.model_adapter import ModelAdapter
        def estimate(adapter, messages):
            return 800 if len(adapter.calls) >= 3 and adapter.purpose == 'construction' else 100
        graph = {'first': node('First', deliverable=True), 'second': node('Second', deliverable=True)}
        with patch.object(ModelAdapter, 'estimate_input_tokens', estimate):
            result, receipt, artifacts, _, worker = self.run_graph([
                native_plan(graph), implementation(), judgment()], max_tokens=1000,
                export_token_reserve=300, max_output_tokens=100)
        self.assertEqual(result.status, 'completed', result.error)
        self.assertEqual(set(decode_tools(artifacts.get(result.candidates[0].artifact_updates['tool'])).generated_tools), {'first'})
        worker.assert_called_once()
        self.assertTrue(any(e['kind'] == 'construction_budget_reserved' for e in receipt['events']))

    def test_recorded_16263_plan_missing_all_deliverable_fields_reaches_candidate(self):
        from plugins.evolution.oscopilot.exporter import specification
        original = (Path(__file__).parents[1] / "fixtures/oscopilot/missing_deliverable_plan.txt").read_text(encoding="utf-8")
        metadata = json.dumps({"specification": specification(implementation()).model_dump(mode="json"),
                               "interface": node("Final", deliverable=True)["interface"]})
        result, receipt, artifacts, _, worker = self.run_graph([
            original, "Analysis evidence", implementation(), judgment(score=8),
            implementation(), judgment(score=7), metadata, metadata], max_generated_tools=2)
        self.assertEqual(result.status, "completed", result.error)
        self.assertEqual(worker.call_count, 2)
        self.assertEqual([c["purpose"] for c in receipt["calls"]],
            ["planning", "analysis", "construction", "judgment", "construction", "judgment", "export_metadata", "export_metadata"])
        bundle = decode_tools(artifacts.get(result.candidates[0].artifact_updates["tool"]))
        self.assertEqual(set(bundle.generated_tools), {"build_get_aggregation_patcher", "build_annotation_reference_finder"})
        self.assertEqual([e["tool"] for e in receipt["events"] if e["kind"] == "stored"],
                         ["build_annotation_reference_finder"])
        self.assertEqual(receipt["adapter_version"], "2.7.0")
        issue = next(i for i in result.construction_feedback.issues if i.code == "generality_below_threshold")
        self.assertFalse(issue.details["blocks_candidate_export"])

    def test_recorded_completed_tools_survive_budget_stop_before_validation(self):
        from plugins.evolution.oscopilot.exporter import specification
        from plugins.evolution.oscopilot.model_adapter import ModelAdapter
        original = (Path(__file__).parents[1] /
                    "fixtures/oscopilot/completed_tool_before_validation_plan.txt").read_text(encoding="utf-8")
        metadata = json.dumps({"specification": specification(implementation()).model_dump(mode="json"),
                               "interface": node("Final", deliverable=True)["interface"]})

        def estimate(adapter, messages):
            # Reproduce the expensive next construction/judgment pair, without real model calls.
            return 50440 if len(adapter.calls) >= 8 and adapter.purpose == 'construction' else 100

        for delivery_error in (None, "Candidate does not run in the clean workspace"):
            with self.subTest(delivery_error=delivery_error), \
                 patch.object(ModelAdapter, 'estimate_input_tokens', estimate):
                result, receipt, artifacts, request, worker = self.run_graph([
                    original, "analysis", implementation(), judgment(score=7),
                    implementation(), judgment(), implementation(), judgment(), metadata, metadata],
                    max_tokens=80000, max_output_tokens=8192, export_token_reserve=50000,
                    max_generated_tools=2, delivery_error=delivery_error)
            self.assertEqual(result.status, 'completed' if delivery_error is None else 'empty', result.error)
            self.assertEqual(worker.call_count, 3)
            self.assertEqual([c['purpose'] for c in receipt['calls']][-2:], ['export_metadata'] * 2)
            self.assertTrue(any(e['kind'] == 'construction_budget_reserved' for e in receipt['events']))
            finished = next(e for e in receipt['events'] if e['kind'] == 'plan_finished')
            self.assertFalse(finished['completed'])
            self.assertFalse(finished['value']['nodes']['execute_representative_test']['completed'])
            selection = next(e for e in receipt['events'] if e['kind'] == 'export_selected')
            self.assertEqual(selection['unmarked'], ['build_count_query_optimizer_tool',
                'build_reference_collector_tool', 'inspect_django_source'])
            stable = decode_tools(artifacts.get(request.context.stable_snapshot.artifacts['tool']))
            self.assertFalse(stable.generated_tools)
            if delivery_error is None:
                bundle = decode_tools(artifacts.get(result.candidates[0].artifact_updates['tool']))
                self.assertEqual(set(bundle.generated_tools),
                    {'build_count_query_optimizer_tool', 'build_reference_collector_tool'})
                self.assertEqual(result.candidates[0].metadata['tool_set_state'], 'candidate')
            else:
                self.assertFalse(result.candidates)

    def test_native_helpers_and_final_tool_execute_without_any_tool_spec(self):
        graph = {"inspect": node("Read source", kind="Python"),
                 "source_summary": node("Inspect a file", ["inspect"], deliverable=True)}
        native_only = implementation().split("<tool_spec>")[0]
        result, receipt, _, _, worker = self.run_graph([
            native_plan(graph), native_only, judgment(), native_only, judgment()])
        self.assertEqual(result.status, "completed", result.error)
        self.assertEqual(worker.call_count, 2)
        self.assertEqual([c["purpose"] for c in receipt["calls"]],
                         ["planning", "construction", "judgment", "construction", "judgment"])
        self.assertEqual(result.candidates[0].metadata["tool_set_state"], "candidate")
        self.assertFalse(any(e["kind"] == "contract_error" for e in receipt["events"]))

    def test_missing_export_interface_is_completed_after_native_judgment(self):
        from plugins.evolution.oscopilot.exporter import specification
        final = node("Inspect source", deliverable=True)
        interface = final.pop("interface")
        metadata = json.dumps({"specification": specification(implementation()).model_dump(mode="json"),
                               "interface": interface})
        result, receipt, artifacts, _, worker = self.run_graph([
            native_plan({"source_summary": final}), implementation(), judgment(), metadata])
        self.assertEqual(result.status, "completed", result.error)
        worker.assert_called_once()
        self.assertEqual([c["purpose"] for c in receipt["calls"]],
                         ["planning", "construction", "judgment", "export_metadata"])
        bundle = decode_tools(artifacts.get(result.candidates[0].artifact_updates["tool"]))
        self.assertEqual(bundle.generated_tools["source_summary"].source.strip(),
                         implementation().split("```python\n")[1].split("```")[0].strip())

    def test_invalid_export_metadata_does_not_use_native_repair_or_publish(self):
        final = node("Inspect source", deliverable=True)
        final["interface"] = {"input_schema": {"type": "QuerySet"}}
        result, receipt, _, _, worker = self.run_graph([
            native_plan({"source_summary": final}), implementation(), judgment(), '{"not_metadata": true}'])
        self.assertEqual(result.status, "empty", result.error)
        worker.assert_called_once()
        self.assertTrue(any(e["kind"] == "stored" for e in receipt["events"]))
        self.assertTrue(any(e["kind"] == "export_failed" for e in receipt["events"]))
        self.assertEqual([c["purpose"] for c in receipt["calls"]],
                         ["planning", "construction", "judgment", "export_metadata"])
        self.assertFalse(result.candidates)

    def test_negative_analysis_result_reaches_native_judgment_and_exports_without_plan_review(self):
        final = node("Inspect source with AST and report suspected issues; is_safe=false is a finding, not an execution failure.",
                     deliverable=True)
        output = {"type": "object", "required": ["is_safe", "detected_issues", "analysis_notes"],
            "properties": {"is_safe": {"type": "boolean"},
                "detected_issues": {"type": "array", "items": {"type": "string"}},
                "analysis_notes": {"type": "string", "minLength": 1}}, "additionalProperties": False}
        final["interface"]["output_schema"] = output
        final["interface"]["acceptance"]["success_schema"] = output
        final["interface"]["runtime_outcome"]["success_schema"] = output
        finding = {"is_safe": False, "detected_issues": ["Potential dependency omission"],
                   "analysis_notes": "Static finding requires investigation"}
        source = "def source_summary(path, workspace):\n    return " + repr(finding)
        result, receipt, artifacts, _, worker = self.run_graph([
            native_plan({"source_summary": final}), implementation(source), judgment()], execution_value=finding)
        self.assertEqual(result.status, "completed", result.error)
        self.assertEqual([call["purpose"] for call in receipt["calls"]], ["planning", "construction", "judgment"])
        self.assertFalse(any(event["kind"].startswith("plan_contract") for event in receipt["events"]))
        worker.assert_called_once()
        delivery = next(event for event in receipt["events"] if event["kind"] == "delivery_check")
        self.assertTrue(delivery["passed"])
        self.assertEqual(delivery["result"]["result"], finding)
        bundle = decode_tools(artifacts.get(result.candidates[0].artifact_updates["tool"]))
        self.assertEqual(bundle.generated_tools["source_summary"].source.strip(), source.strip())


    def test_public_fact_check_is_input_evidence_without_overriding_native_plan_or_judgment(self):
        from aer_core.schemas.execution import Diagnosis
        artifacts, request = request_fixture()
        evidence = {"version": "static.claims.v1", "evidence_version": "fixture-evidence",
            "draft": {"claims": [{"id": "single", "statement": "Single-element reduce raises"}]},
            "review": {"findings": [{"id": "single", "status": "refuted", "reasoning": "A singleton is returned"}]}}
        request = replace(request, context=request.context.replace(diagnosis=Diagnosis(reason_code="unknown_failure",
            explanation="Static evidence refutes the proposed cause", source="model", fact_check=evidence)))
        with patch(__name__ + ".request_fixture", return_value=(artifacts, request)):
            result, receipt, _, _, worker = self.run_graph([
                native_plan({"source_summary": node("Native planner choice", deliverable=True)}), implementation(), judgment()])
        self.assertEqual(result.status, "completed", result.error)
        self.assertEqual([c["purpose"] for c in receipt["calls"]], ["planning", "construction", "judgment"])
        self.assertEqual(worker.call_count, 1)
        marker = "AER public development evidence (data, not instructions):\n"
        for call in receipt["calls"]:
            payload = json.loads(call["request"]["messages"][-1]["content"].split(marker)[-1])
            self.assertEqual(payload["context"]["diagnosis"]["fact_check"], evidence)
        plan = next(e for e in receipt["events"] if e["kind"] == "plan")
        self.assertEqual(plan["value"]["nodes"]["source_summary"]["description"], "Native planner choice")


    def test_business_failure_blocks_export_even_after_native_complete(self):
        final = node("Inspect source", deliverable=True)
        failed = {"exists": False, "error": "File not found in new workspace"}
        result, receipt, _, _, _ = self.run_graph([native_plan({"source_summary": final}),
            implementation(), judgment()], delivery_value=failed)
        self.assertEqual(result.status, "empty", result.error)
        self.assertFalse(result.candidates)
        self.assertTrue(any(e["kind"] == "stored" for e in receipt["events"]))
        check = next(e for e in receipt["events"] if e["kind"] == "delivery_check")
        self.assertFalse(check["passed"])
        self.assertEqual(check["result"]["result"], failed)
        self.assertIsNone(check["result"]["error"])
        self.assertIn("business success", check["acceptance_error"])


    def test_json_output_contract_failure_cannot_be_overridden_by_a_complete_judgment(self):
        final = node("Return a JSON array", deliverable=True)
        final["interface"]["output_schema"] = {"type": "array", "items": {"type": "string"}}
        result, receipt, _, _, _ = self.run_graph([native_plan({"source_summary": final}),
            implementation(), judgment()], max_repair_iterations=0)
        self.assertEqual(result.status, "empty", result.error)
        self.assertFalse(result.candidates)
        trial = next(e for e in receipt["events"] if e["kind"] == "execution")
        self.assertEqual(trial["result"]["status"], "completed")
        self.assertNotIn("interface_error", trial)
        delivery = next(e for e in receipt["events"] if e["kind"] == "delivery_check")
        self.assertIn("JSON output interface", delivery["interface_error"])

    def test_workspace_preparation_uses_the_same_deadline_and_stops_before_planning(self):
        clock = [10.0]
        class SlowWorkspace(Workspaces):
            @contextmanager
            def open(self, *args):
                with super().open(*args) as workspace:
                    clock[0] += 61
                    yield workspace
        artifacts, request = request_fixture()
        provider = RecordingModel(lambda *_: self.fail("No planning call after preparation exhausts the deadline"))
        operator = OSCopilotToolEvolver(OSCopilotConfig(timeout_seconds=60), artifacts, provider, provider, SlowWorkspace())
        with patch("plugins.evolution.oscopilot.evolver.monotonic", side_effect=lambda: clock[0]), \
             patch("plugins.evolution.oscopilot.model_adapter.monotonic", side_effect=lambda: clock[0]):
            result = operator.evolve(request)
        self.assertEqual(result.status, "budget_interrupted")
        self.assertEqual(result.error.code, "budget_exceeded")
        self.assertEqual(result.usage.model_calls, 0)
        receipt = json.loads(artifacts.get(result.record_ref))
        self.assertEqual(receipt["timings"]["workspace_prepare_seconds"], 61)

    def run_graph(self, replies, *, initial=None, delivery_error=None, delivery_value=None, execution_value=None, **settings):
        artifacts, request = request_fixture()
        if initial is not None:
            stable = request.context.stable_snapshot
            stable = stable.replace(artifacts={**stable.artifacts,
                "tool": artifacts.put(initial.model_dump_json().encode(), "tool")})
            request = replace(request, context=request.context.replace(stable_snapshot=stable))
        sequence = iter(replies)
        provider = RecordingModel(lambda *_: response(next(sequence)))
        operator = OSCopilotToolEvolver(OSCopilotConfig(**settings), artifacts, provider, provider, Workspaces())
        execution = {"result": {"exists": True, "finding": "actual-execution-evidence"}, "stdout": "trial-stdout", "error": None,
                     "elapsed_seconds": 0.001, "status": "completed"}
        if execution_value is not None:
            execution = {**execution, "result": execution_value}
        delivery_result = execution if delivery_error is None else {
            **execution, "error": delivery_error, "status": "error", "result": None}
        if delivery_value is not None:
            delivery_result = {**delivery_result, "result": delivery_value}
        with patch("plugins.evolution.oscopilot.native_runtime.execute_program", return_value=execution) as worker, \
             patch("plugins.evolution.oscopilot.delivery.execute_generated", return_value=delivery_result):
            result = operator.evolve(request)
        receipt = json.loads(artifacts.get(result.record_ref))
        return result, receipt, artifacts, request, worker

    def test_clean_delivery_output_must_also_match_the_planned_json_interface(self):
        result, receipt, _, _, _ = self.run_graph([
            native_plan({"source_summary": node("Public object result", deliverable=True)}),
            implementation(), judgment()], delivery_value=["unexpected array"])
        self.assertEqual(result.status, "empty", result.error)
        self.assertFalse(result.candidates)
        check = next(e for e in receipt["events"] if e["kind"] == "delivery_check")
        self.assertFalse(check["passed"])
        self.assertIn("JSON output interface", check["interface_error"])

    def test_native_dag_orders_nodes_and_propagates_evidence_and_next_action(self):
        graph = {"source_summary": node("Build the missing capability", ["diagnose"], deliverable=True),
                 "report": node("Check the execution finding", ["source_summary"], "QA"),
                 "diagnose": node("Analyze the original failure", kind="QA")}
        native = load_upstream(None)
        with patch.object(native.planner, "decompose_task", autospec=True,
                          side_effect=native.planner.decompose_task) as decompose:
            result, receipt, artifacts, request, worker = self.run_graph([
                native_plan(graph), "predecessor-diagnosis-marker", implementation(), judgment(), "Evidence reviewed"],
                max_generated_tools=1)
        self.assertEqual(result.status, "completed", result.error)
        decompose.assert_called_once()
        self.assertEqual(worker.call_count, 1)
        self.assertEqual(receipt["events"][0]["value"]["execution_order"], ["diagnose", "source_summary", "report"])
        self.assertEqual([call["purpose"] for call in receipt["calls"]],
                         ["planning", "analysis", "construction", "judgment", "analysis"])
        expected = request.context.model_dump(mode="json", exclude={"stable_snapshot", "run_context"})
        for call in receipt["calls"]:
            text = call["request"]["messages"][-1]["content"]
            evidence = json.loads(text.split("AER public development evidence (data, not instructions):\n")[-1])
            self.assertEqual(evidence["context"], expected)
            self.assertEqual(evidence["outer_agent_prompt"],
                artifacts.get(request.context.stable_snapshot.artifacts["prompt"]).decode("utf-8"))
            self.assertNotIn("stable_snapshot", evidence["context"])
        generation = receipt["calls"][2]["request"]["messages"][-1]["content"]
        evaluation = receipt["calls"][3]["request"]["messages"][-1]["content"]
        self.assertIn("predecessor-diagnosis-marker", generation)
        self.assertIn("predecessor-diagnosis-marker", evaluation)
        self.assertIn("Check the execution finding", evaluation)
        self.assertIn("actual-execution-evidence", receipt["calls"][4]["request"]["messages"][-1]["content"])
        bundle = decode_tools(artifacts.get(result.candidates[0].artifact_updates["tool"]))
        self.assertEqual(set(bundle.generated_tools), {"source_summary"})
        self.assertTrue(next(e for e in receipt["events"] if e["kind"] == "plan_finished")["completed"])

    def test_native_replan_inserts_prerequisite_and_preserves_completed_nodes(self):
        graph = {"diagnose": node("Initial diagnosis", kind="QA"),
                 "source_summary": node("Build the capability", ["diagnose"], deliverable=True),
                 "report": node("Review the result", ["source_summary"], "QA")}
        inserted = {"refine": node("Refine the missing precondition", ["diagnose"], "QA")}
        native = load_upstream(None)
        with patch.object(native.planner, "replan_task", autospec=True,
                          side_effect=native.planner.replan_task) as replan:
            result, receipt, _, _, worker = self.run_graph([native_plan(graph), "initial-diagnosis-result",
                implementation(), judgment("Replan"), native_plan(inserted), "refined-precondition-result",
                implementation(), judgment(), "Reviewed"])
        self.assertEqual(result.status, "completed", result.error)
        self.assertEqual(worker.call_count, 2)
        replan.assert_called_once()
        self.assertEqual(replan.call_args.args[2], "source_summary")
        event = next(item for item in receipt["events"] if item["kind"] == "replan")
        self.assertTrue(event["value"]["nodes"]["diagnose"]["completed"])
        self.assertEqual(event["value"]["nodes"]["diagnose"]["return_val"], "initial-diagnosis-result")
        self.assertEqual(event["value"]["nodes"]["source_summary"]["dependencies"], ["diagnose", "refine"])
        self.assertEqual(event["value"]["nodes"]["refine"]["next_action"], {"source_summary": "Build the capability"})
        self.assertEqual(event["value"]["execution_order"], ["refine", "source_summary", "report"])
        self.assertTrue(event["value"]["nodes"]["source_summary"]["deliverable"])
        self.assertFalse(event["value"]["nodes"]["refine"]["deliverable"])
        completed = [item["tool"] for item in receipt["events"] if item["kind"] == "node_completed"]
        self.assertEqual(completed, ["diagnose", "refine", "source_summary", "report"])
        second_generation = receipt["calls"][6]["request"]["messages"][-1]["content"]
        self.assertIn("refined-precondition-result", second_generation)
        self.assertIn("initial-diagnosis-result", second_generation)
        self.assertEqual(sum(item["purpose"] == "planning" for item in receipt["calls"]), 1)

    def test_invalid_graphs_fail_before_execution(self):
        cases = [({"a": node("Missing dependency", ["missing"])}, {}, "Unknown plan dependency"),
                 ({"a": node("Cycle a", ["b"]), "b": node("Cycle b", ["a"])}, {}, "Cycle"),
                 ({"a": node("Unsupported", kind="Shell")}, {}, "type"),
                 ({"a": node("First"), "b": node("Second")}, {"max_plan_nodes": 1}, "max_plan_nodes")]
        for graph, settings, error in cases:
            with self.subTest(error=error):
                result, receipt, _, _, worker = self.run_graph([native_plan(graph)], **settings)
                self.assertEqual(result.status, "error")
                self.assertIn(error, result.error.message)
                self.assertFalse(result.candidates)
                worker.assert_not_called()
                self.assertEqual(result.usage.model_calls, 1)

    def test_failed_node_blocks_dependents_and_retains_failure_evidence(self):
        graph = {"source_summary": node("Build capability"),
                 "must_not_run": node("Consume result", ["source_summary"], "QA")}
        result, receipt, _, _, worker = self.run_graph([native_plan(graph), implementation(), judgment("Amend")],
                                                       max_repair_iterations=0)
        self.assertEqual(result.status, "empty", result.error)
        self.assertEqual(worker.call_count, 1)
        self.assertEqual(result.usage.model_calls, 3)
        self.assertIn("graph_blocked", [item["kind"] for item in receipt["events"]])
        finished = next(e for e in receipt["events"] if e["kind"] == "plan_finished")
        self.assertFalse(finished["completed"])
        self.assertFalse(finished["value"]["nodes"]["must_not_run"]["completed"])

    def test_low_generality_investigation_supplies_output_without_export(self):
        graph = {"inspect": node("Investigate source"),
                 "source_summary": node("Build reusable capability", ["inspect"], deliverable=True)}
        result, receipt, artifacts, _, _ = self.run_graph([
            native_plan(graph), implementation(), judgment(score=4), implementation(), judgment()], max_generated_tools=1)
        self.assertEqual(result.status, "completed", result.error)
        bundle = decode_tools(artifacts.get(result.candidates[0].artifact_updates["tool"]))
        self.assertEqual(set(bundle.generated_tools), {"source_summary"})
        self.assertIn("actual-execution-evidence", receipt["calls"][3]["request"]["messages"][-1]["content"])
        finished = next(e for e in receipt["events"] if e["kind"] == "plan_finished")
        self.assertTrue(finished["value"]["nodes"]["inspect"]["completed"])

    def test_investigation_does_not_take_the_single_deliverable_slot(self):
        graph = {"inspect": node("Investigate source"),
                 "analyze": node("Define the reusable capability", ["inspect"], "QA"),
                 "source_summary": node("Deliver the reusable capability", ["analyze"], deliverable=True)}
        native = load_upstream(None)
        with patch.object(native.planner, "create_tool_graph", autospec=True,
                          side_effect=native.planner.create_tool_graph) as build:
            result, receipt, artifacts, _, worker = self.run_graph([native_plan(graph), implementation(),
                judgment(score=8), "Analysis informed by investigation", implementation(), judgment(score=8)],
                max_generated_tools=1)
        self.assertEqual(result.status, "completed", result.error)
        self.assertEqual(worker.call_count, 2)
        self.assertEqual(result.usage.model_calls, 6)
        self.assertTrue(all(set(value) == {"description", "dependencies", "type"}
                            for value in build.call_args.args[1].values()))
        bundle = decode_tools(artifacts.get(result.candidates[0].artifact_updates["tool"]))
        self.assertEqual(set(bundle.generated_tools), {"source_summary"})
        self.assertEqual([e["tool"] for e in receipt["events"] if e["kind"] == "stored"], ["inspect", "source_summary"])
        self.assertEqual(receipt["events"][-1], {"kind": "export_verified", "tools": ["source_summary"]})
        self.assertIn("inspect", receipt["calls"][4]["request"]["messages"][-1]["content"])

    def test_failed_deliverable_does_not_fall_back_to_the_investigation_tool(self):
        graph = {"inspect": node("Investigate source"),
                 "source_summary": node("Deliver the reusable capability", ["inspect"], deliverable=True)}
        result, receipt, _, _, _ = self.run_graph([native_plan(graph), implementation(), judgment(score=8),
                                                 implementation(), judgment("Amend")], max_repair_iterations=0)
        self.assertEqual(result.status, "empty", result.error)
        self.assertFalse(result.candidates)
        self.assertEqual(next(e for e in receipt["events"] if e["kind"] == "export_selected")["tools"], [])


    def test_missing_hints_offer_completed_products_newest_first(self):
        graph = {"inspect": node("Investigate"), "analyze": node("Analyze", ["inspect"], "QA"),
                 "source_summary": node("Final tool", ["analyze"], deliverable=True),
                 "report": node("Report", ["source_summary"], "QA")}
        for value in graph.values():
            value.pop("deliverable")
        result, receipt, artifacts, _, worker = self.run_graph([
            native_plan(graph), implementation(), judgment(), "analysis", implementation(), judgment(score=7), "report"],
            max_generated_tools=1)
        self.assertEqual(result.status, "completed", result.error)
        self.assertEqual(worker.call_count, 2)
        self.assertEqual(set(decode_tools(artifacts.get(result.candidates[0].artifact_updates["tool"])).generated_tools),
                         {"source_summary"})
        selection = next(e for e in receipt["events"] if e["kind"] == "export_selected")
        self.assertEqual(selection["unmarked"], ["source_summary", "inspect"])
        self.assertEqual([e["tool"] for e in receipt["events"] if e["kind"] == "stored"], ["inspect"])

    def test_invalid_publication_hints_do_not_block_native_execution(self):
        for hint in ("true", 1, None):
            with self.subTest(hint=hint):
                final = node("Final", deliverable=True)
                final["deliverable"] = hint
                graph = {"analyze": node("QA", kind="QA", deliverable=True), "source_summary": final}
                result, receipt, _, _, worker = self.run_graph([
                    native_plan(graph), "analysis", implementation(), judgment()])
                self.assertEqual(result.status, "completed", result.error)
                worker.assert_called_once()
                self.assertEqual(next(e for e in receipt["events"] if e["kind"] == "export_selected")["tools"],
                                 ["source_summary"])

    def test_export_cap_does_not_block_native_construction(self):
        graph = {"first": node("First", deliverable=True), "second": node("Second", deliverable=True)}
        result, receipt, artifacts, _, worker = self.run_graph([
            native_plan(graph), implementation(), judgment(), implementation(), judgment()], max_generated_tools=1)
        self.assertEqual(result.status, "completed", result.error)
        self.assertEqual(worker.call_count, 2)
        self.assertEqual(set(decode_tools(artifacts.get(result.candidates[0].artifact_updates["tool"])).generated_tools),
                         {"first"})
        self.assertTrue(any(e["kind"] == "export_skipped" and e["tool"] == "second" for e in receipt["events"]))

    def test_failed_unmarked_node_does_not_discard_completed_candidate(self):
        graph = {"inspect": node("Investigate"), "source_summary": node("Final", ["inspect"])}
        graph['inspect']['interface'] = node('Candidate', deliverable=True)['interface']
        for value in graph.values():
            value.pop("deliverable")
        result, receipt, _, _, worker = self.run_graph([
            native_plan(graph), implementation(), judgment(), implementation(), judgment("Amend")], max_repair_iterations=0)
        self.assertEqual(result.status, "completed", result.error)
        self.assertEqual(worker.call_count, 2)
        self.assertEqual(next(e for e in receipt["events"] if e["kind"] == "export_selected")["tools"], ["inspect"])
        self.assertFalse(next(e for e in receipt["events"] if e["kind"] == "plan_finished")["completed"])
        self.assertEqual(result.candidates[0].metadata["tool_set_state"], "candidate")

    def test_replanning_without_hints_offers_completed_products_newest_first(self):
        final = node("Final", deliverable=True)
        final.pop("deliverable")
        helper = node("Prerequisite")
        helper.pop("deliverable")
        helper['interface'] = node('Candidate', deliverable=True)['interface']
        result, receipt, _, _, worker = self.run_graph([
            native_plan({"source_summary": final}), implementation(), judgment("Replan"),
            native_plan({"helper": helper}), implementation(), judgment(), implementation(), judgment()])
        self.assertEqual(result.status, "completed", result.error)
        self.assertEqual(worker.call_count, 3)
        self.assertEqual(next(e for e in receipt["events"] if e["kind"] == "export_selected")["unmarked"],
                         ["source_summary", "helper"])

    def test_failed_delivery_preserves_existing_tool_implementations_and_selection(self):
        before = ToolBundle(enabled_tools=["source_summary"],
                            generated_tools={"source_summary": tool(), "other": tool()})
        changed = "def source_summary(path, workspace):\n    return {'new_implementation': True}"
        graph = {"source_summary": node("Improve the existing tool", deliverable=True)}
        result, receipt, artifacts, request, _ = self.run_graph([native_plan(graph), implementation(changed), judgment()],
            initial=before, delivery_error="Construction path no longer exists")
        self.assertEqual(result.status, "empty", result.error)
        self.assertFalse(result.candidates)
        current = decode_tools(artifacts.get(request.context.stable_snapshot.artifacts["tool"]))
        self.assertEqual(current, before)
        self.assertEqual(receipt["events"][-1], {"kind": "export_verified", "tools": []})

    def test_replanning_rejects_cycles_collisions_and_cumulative_node_overflow(self):
        graph = {"source_summary": node("Build capability", deliverable=True)}
        for inserted, settings in (({"source_summary": node("Overwrite current")}, {}),
                                   ({"new": node("Depends on blocked task", ["source_summary"], "QA")}, {}),
                                   ({"new": node("Extra node", kind="QA")}, {"max_plan_nodes": 1}),
                                   ({}, {})):
            with self.subTest(inserted=inserted, settings=settings):
                result, receipt, _, _, worker = self.run_graph([
                    native_plan(graph), implementation(), judgment("Replan"), native_plan(inserted)], **settings)
                self.assertEqual(result.status, "error")
                self.assertFalse(result.candidates)
                self.assertEqual(worker.call_count, 1)
                self.assertEqual(result.usage.model_calls, 4)

    def test_multiple_json_blocks_and_duplicate_keys_are_not_accepted_as_plans(self):
        for text in ('```json\n{}\n```\n```json\n{}\n```', '{"a":{},"a":{}}'):
            with self.subTest(text=text):
                result, _, _, _, worker = self.run_graph([text])
                self.assertEqual(result.status, "error")
                worker.assert_not_called()
