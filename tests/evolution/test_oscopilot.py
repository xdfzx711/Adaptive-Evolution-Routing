from contextlib import contextmanager
import importlib.util
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from aer_core.schemas.evolution import EvolutionRequest
from plugins.evolution.oscopilot.config import OSCopilotConfig
from plugins.evolution.oscopilot.evolver import OSCopilotToolEvolver
from plugins.evolution.oscopilot.source import load_upstream, DEFAULT_SOURCE
from plugins.tools.catalog import ToolingConfig
from plugins.tools.generated.bundle import ToolBundle, decode_tools
from plugins.tools.generated.runtime import execute_generated
from tests.evolution import test_tool_selection
from tests.execution.helpers import RecordingModel, response
from tests.tools.test_generated import SOURCE, SCHEMA


HAS_RUNTIME = importlib.util.find_spec("ipykernel") and (DEFAULT_SOURCE / "oscopilot").is_dir()


def plan():
    return native_plan({"source_summary": node("Inspect a file in the current repository", deliverable=True)})


def node(description, dependencies=(), kind="Python", *, deliverable=False):
    value = {"description": description, "dependencies": list(dependencies), "type": kind, "deliverable": deliverable}
    if deliverable:
        value["interface"] = {"input_schema": SCHEMA, "output_schema": {"type": "object"},
            "usage": {"capability_gap": "Repository inspection evidence", "use_when": "Check source availability",
                "added_value": "Reusable structured inspection", "result_usage": "Use the returned evidence to select source",
                "preconditions": "Repository-relative path", "cost": "One local file inspection"},
            "runtime_outcome": {"success_schema": {"type": "object", "required": ["exists"], "properties": {"exists": {"type": "boolean"}}},
                "failure_schema": {"type": "object", "required": ["error"], "properties": {"error": {"type": "string"}}}},
            "acceptance": {"kind": "json", "patch_pointer": None, "success_schema": {"type": "object",
                "required": ["exists"], "properties": {"exists": {"const": True}}}}}
    return value


def native_plan(nodes):
    return "Analyze the observed failure and construct reusable capabilities.\n```json\n" + json.dumps(nodes) + "\n```"


def implementation(source=SOURCE):
    spec = {"description": "Inspect a file in the current repository", "entrypoint": "source_summary", "kind": "function",
            "input_schema": SCHEMA, "constructor_arguments": {}, "example_arguments": {"path": "sample.py"}}
    return "```python\n" + source + "\n```\n<invoke>source_summary('sample.py', workspace=workspace)</invoke>\n<tool_spec>" + json.dumps(spec) + "</tool_spec>"


def judgment(status="Complete", score=9):
    return "```json\n" + json.dumps({"reasoning": "Observed execution evidence", "status": status, "score": score}, indent=2) + "\n```"


class Workspaces:
    def __init__(self):
        self.last_path = None

    @contextmanager
    def open(self, task, snapshot, context):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "task"
            workspace.mkdir()
            (workspace / "sample.py").write_text("def example(): return 1", encoding="utf-8")
            self.last_path = workspace
            yield SimpleNamespace(path=workspace, repository=root / "repository",
                tooling=ToolingConfig(catalog={}, generated_tools={"enabled": True}))


def request_fixture():
    artifacts, request = test_tool_selection.ToolEvolverTests().request(catalog={})
    stable = request.context.stable_snapshot
    tool_ref = artifacts.put(ToolBundle().model_dump_json().encode(), "tool")
    stable = stable.replace(config={"tool_catalog": {}, "generated_tools_enabled": True,
        "tooling": {"catalog": {}, "generated_tools": {"enabled": True}}},
        artifacts={**stable.artifacts, "tool": tool_ref})
    return artifacts, EvolutionRequest(attempt_id=request.attempt_id, round_id=1,
        context=request.context.replace(stable_snapshot=stable), decision=request.decision,
        operator_id="oscopilot_tool_evolver", max_candidates=1, run_context=request.run_context)


@unittest.skipUnless(HAS_RUNTIME, "initialize the OS-Copilot submodule and install [oscopilot]")
class OSCopilotTests(unittest.TestCase):
    def test_parameterized_tool_and_usage_survive_export_and_fresh_workspace_invocation(self):
        # Controlled model output: verify the delivery path, not model-generation quality.
        source = '''def trace_dependencies(definitions, roots):
    """Return transitive dependencies for caller-supplied definitions and root names."""
    found, missing, pending = set(), set(), list(roots)
    while pending:
        name = pending.pop()
        if name in found or name in missing:
            continue
        if name not in definitions:
            missing.add(name)
            continue
        found.add(name)
        pending.extend(definitions[name])
    return {'reachable': sorted(found), 'missing': sorted(missing)}
'''
        schema = {'type': 'object', 'properties': {
            'definitions': {'type': 'object', 'description': 'Names mapped to their direct dependency names.',
                            'additionalProperties': {'type': 'array', 'items': {'type': 'string'}}},
            'roots': {'type': 'array', 'items': {'type': 'string'},
                      'description': 'Names whose transitive dependencies should be preserved.'}},
            'required': ['definitions', 'roots'], 'additionalProperties': False}
        output = {'type': 'object', 'required': ['reachable', 'missing'], 'properties': {
            'reachable': {'type': 'array', 'items': {'type': 'string'}},
            'missing': {'type': 'array', 'items': {'type': 'string'}}}, 'additionalProperties': False}
        usage = {'capability_gap': 'Transitive dependency analysis for caller-supplied definitions.',
            'use_when': 'Before pruning entries, supply definitions and the roots that must remain.',
            'added_value': 'Computes cycle-safe transitive closure instead of manual source searches.',
            'result_usage': 'Preserve reachable names; inspect missing names before changing the repository.',
            'preconditions': 'Caller extracts direct dependencies; no repository writes or framework inference.',
            'cost': 'Local traversal of reachable definitions.'}
        example = {'definitions': {'derived': ['base'], 'base': [], 'unused': []}, 'roots': ['derived']}
        interface = {'input_schema': schema, 'output_schema': output, 'usage': usage,
            'runtime_outcome': {'success_schema': output,
                'failure_schema': {'type': 'object', 'required': ['error']}},
            'acceptance': {'kind': 'json', 'success_schema': {'type': 'object',
                'required': ['reachable', 'missing'], 'properties': {
                    'reachable': {'const': ['base', 'derived']}, 'missing': {'const': []}}}}}
        spec = {'description': 'Trace dependencies from supplied roots; return reachable and missing names.',
            'entrypoint': 'trace_dependencies', 'input_schema': schema, 'example_arguments': example}
        final = node('Trace caller-supplied dependencies, returning reachable and missing names.', deliverable=True)
        final.pop('interface')
        code_response = '```python\n' + source + '\n```\n<invoke>trace_dependencies(' + repr(example['definitions']) + ", ['derived'])</invoke>"
        artifacts, request = request_fixture()
        replies = iter([native_plan({'trace_dependencies': final}), code_response, judgment(),
                        json.dumps({'specification': spec, 'interface': interface})])
        model = RecordingModel(lambda *_: response(next(replies)))
        result = OSCopilotToolEvolver(OSCopilotConfig(), artifacts, model, model, Workspaces()).evolve(request)
        self.assertEqual(result.status, 'completed', result.error)
        receipt = json.loads(artifacts.get(result.record_ref))
        self.assertEqual([c['purpose'] for c in receipt['calls']],
                         ['planning', 'construction', 'judgment', 'export_metadata'])
        delivery = next(e for e in receipt['events'] if e['kind'] == 'delivery_check')
        self.assertTrue(delivery['passed'])
        bundle = decode_tools(artifacts.get(result.candidates[0].artifact_updates['tool']))
        tool = bundle.generated_tools['trace_dependencies']
        self.assertEqual(tool.source.strip(), source.strip())
        descriptor = tool.descriptor()
        self.assertEqual(set(descriptor.input_schema['required']), {'definitions', 'roots'})
        for text in usage.values():
            self.assertIn(text, descriptor.description)
        self.assertIn('JSON example', descriptor.description)
        with Workspaces().open(None, None, None) as outer:
            actual = execute_generated(tool, {'definitions': {'x': ['y'], 'y': ['x']}, 'roots': ['y', 'unknown']}, outer.path)
        self.assertIsNone(actual['error'], actual)
        self.assertEqual(actual['result'], {'reachable': ['x', 'y'], 'missing': ['unknown']})
        self.assertNotEqual(actual['result'], delivery['result']['result'])
        self.assertFalse(decode_tools(artifacts.get(request.context.stable_snapshot.artifacts['tool'])).generated_tools)

    def test_absolute_construction_example_survives_delivery_and_a_third_runtime_workspace(self):
        artifacts, request = request_fixture()
        workspaces = Workspaces()
        final = copy.deepcopy(node("Inspect current repository", deliverable=True))
        final["interface"]["input_schema"]["properties"]["path"]["x-aer-path"] = "repository"
        def generated():
            spec = json.loads(implementation().split("<tool_spec>")[1].split("</tool_spec>")[0])
            spec["input_schema"] = final["interface"]["input_schema"]
            spec["example_arguments"]["path"] = str(workspaces.last_path / "sample.py")
            return implementation().split("<tool_spec>")[0] + "<tool_spec>" + json.dumps(spec) + "</tool_spec>"
        replies = iter([lambda: native_plan({"source_summary": final}), generated, judgment])
        model = RecordingModel(lambda *_: response(next(replies)()))
        result = OSCopilotToolEvolver(OSCopilotConfig(), artifacts, model, model, workspaces).evolve(request)
        self.assertEqual(result.status, "completed", result.error)
        receipt = json.loads(artifacts.get(result.record_ref))
        binding = next(e for e in receipt["events"] if e["kind"] == "path_arguments_bound")
        delivery = next(e for e in receipt["events"] if e["kind"] == "delivery_check")
        self.assertEqual(binding["arguments"], {"path": "sample.py"})
        self.assertFalse(Path(binding["workspace"]).exists())
        self.assertTrue(delivery["passed"])
        self.assertEqual(delivery["arguments"], {"path": "sample.py"})
        self.assertNotEqual(binding["bindings"][0]["resolved"], delivery["path_bindings"][0]["resolved"])
        bundle = decode_tools(artifacts.get(result.candidates[0].artifact_updates["tool"]))
        with Workspaces().open(None, None, None) as outer:
            actual = execute_generated(bundle.generated_tools["source_summary"],
                {"path": str(outer.path / "sample.py")}, outer.path)
            self.assertIsNone(actual["error"], actual)
            self.assertTrue(actual["result"]["exists"])
            self.assertEqual(Path(actual["result"]["workspace"]), outer.path)
            self.assertNotEqual(str(outer.path), delivery["workspace"])

    def test_hardcoded_source_is_rejected_at_export_without_consuming_native_repairs(self):
        artifacts, request = request_fixture()
        workspaces = Workspaces()
        def hardcoded():
            source = "def source_summary(path, workspace):\n    from pathlib import Path\n    return {'exists': Path(" + repr(str(workspaces.last_path / "sample.py")) + ").exists()}"
            return implementation(source)
        replies = iter([plan, hardcoded, judgment])
        model = RecordingModel(lambda *_: response(next(replies)()))
        result = OSCopilotToolEvolver(OSCopilotConfig(), artifacts, model, model, workspaces).evolve(request)
        self.assertEqual(result.status, "empty", result.error)
        receipt = json.loads(artifacts.get(result.record_ref))
        self.assertEqual([c["purpose"] for c in receipt["calls"]], ["planning", "construction", "judgment"])
        self.assertIn("Non-portable construction path", next(e for e in receipt["events"] if e["kind"] == "export_failed")["error"])
        self.assertEqual(result.usage.tool_calls, 1)
        self.assertFalse(result.candidates)

    def test_caught_missing_construction_file_cannot_be_published_as_success(self):
        helper = "def source_summary(path, workspace):\n    from pathlib import Path\n    Path(workspace, 'private.txt').write_text('evidence')\n    return {'prepared': True}"
        dependent = '''def source_summary(path, workspace):
    from pathlib import Path
    try:
        return {'success': True, 'content': Path(workspace, 'private.txt').read_text(), 'error_message': None}
    except OSError as exc:
        return {'success': False, 'content': '', 'error_message': str(exc)}
'''
        final = node("Read the evidence independently", ["prepare"], deliverable=True)
        final["interface"]["acceptance"] = {"kind": "json", "success_schema": {"type": "object",
            "required": ["success", "content", "error_message"], "properties": {
                "success": {"const": True}, "content": {"type": "string", "minLength": 1},
                "error_message": {"type": "null"}}}}
        graph = {"prepare": node("Prepare private construction evidence"), "source_summary": final}
        result, receipt, _, _ = self.evolve([native_plan(graph), implementation(helper), judgment(),
            implementation(dependent), judgment()], max_generated_tools=1)
        self.assertEqual(result.status, "empty", result.error)
        self.assertFalse(result.candidates)
        check = next(e for e in receipt["events"] if e["kind"] == "delivery_check")
        self.assertFalse(check["passed"])
        self.assertEqual(check["result"]["status"], "completed")
        self.assertIsNone(check["result"]["error"])
        self.assertFalse(check["result"]["result"]["success"])
        self.assertIn("private.txt", check["result"]["result"]["error_message"])
        self.assertIn("business success", check["acceptance_error"])

    def test_workspace_metadata_is_adapted_and_runtime_injects_the_fresh_workspace(self):
        public = json.loads(implementation().split("<tool_spec>")[1].split("</tool_spec>")[0])
        public["input_schema"]["properties"]["workspace"] = {"type": "string"}
        public["input_schema"]["required"].append("workspace")
        public["example_arguments"]["workspace"] = "C:/construction-path-that-must-not-be-used"
        generated = implementation().split("<tool_spec>")[0] + "<tool_spec>" + json.dumps(public) + "</tool_spec>"
        result, receipt, artifacts, _ = self.evolve([plan(), generated, judgment()])
        self.assertEqual(result.status, "completed", result.error)
        self.assertEqual(result.usage.model_calls, 3)
        self.assertFalse(any(call["purpose"] == "repair" for call in receipt["calls"]))
        self.assertTrue(any(e["kind"] == "delivery_metadata_adapted" for e in receipt["events"]))
        bundle = decode_tools(artifacts.get(result.candidates[0].artifact_updates["tool"]))
        self.assertEqual(bundle.generated_tools["source_summary"].source.strip(), SOURCE.strip())
        schema = bundle.generated_tools["source_summary"].model_dump(mode="json")["input_schema"]
        self.assertNotIn("workspace", schema["properties"])
        self.assertNotIn("workspace", schema["required"])
        trial = next(e for e in receipt["events"] if e["kind"] == "execution")
        self.assertNotEqual(trial["result"]["result"]["workspace"], public["example_arguments"]["workspace"])
        self.assertNotIn("workspace", bundle.generated_tools["source_summary"].usage_example)

    def evolve(self, replies, **config):
        artifacts, request = request_fixture()
        sequence = iter(replies)
        model = RecordingModel(lambda *_: response(next(sequence)))
        workspace = Workspaces()
        operator = OSCopilotToolEvolver(OSCopilotConfig(**config), artifacts, model, model, workspace)
        result = operator.evolve(request)
        receipt = json.loads(artifacts.get(result.record_ref))
        self.assertFalse(workspace.last_path.exists() if workspace.last_path else False)
        return result, receipt, artifacts, request

    def test_native_generation_execution_judgment_repair_and_exact_repaired_storage(self):
        broken = "def source_summary(path, workspace):\n    raise ValueError('repair me')"
        result, receipt, artifacts, request = self.evolve([plan(), implementation(broken), judgment("Amend"),
                                                          implementation(), judgment()])
        self.assertEqual(result.status, "completed", result.error)
        bundle = decode_tools(artifacts.get(result.candidates[0].artifact_updates["tool"]))
        self.assertEqual(bundle.generated_tools["source_summary"].source.strip(), SOURCE.strip())
        self.assertNotIn("repair me", bundle.generated_tools["source_summary"].source)
        self.assertEqual(result.usage.model_calls, 5)
        self.assertEqual(result.usage.tool_calls, 3)
        self.assertEqual([event["kind"] for event in receipt["events"]],
                         ["plan", "execution", "judgment", "execution", "judgment", "stored", "node_completed",
                          "plan_finished", "export_selected", "candidate_tools_prepared", "delivery_check", "export_verified"])
        native = load_upstream()
        self.assertIn("third_party", native.executor.generate_tool.__code__.co_filename)
        self.assertIn("third_party", native.planner.decompose_task.__code__.co_filename)
        self.assertIn("third_party", native.planner.replan_task.__code__.co_filename)
        self.assertEqual(set(result.candidates[0].artifact_updates), {"tool"})
        trial = next(e for e in receipt["events"] if e["kind"] == "execution" and e["result"]["error"] is None)
        delivery = next(e for e in receipt["events"] if e["kind"] == "delivery_check")
        self.assertNotEqual(trial["result"]["result"]["workspace"], delivery["workspace"])
        self.assertFalse(Path(trial["result"]["result"]["workspace"]).exists())
        self.assertTrue(delivery["passed"])

    def test_native_syntax_repair_with_prose_tags_and_missing_fence_executes_and_exports(self):
        broken = "def source_summary(path, workspace)\n    return {}"
        repaired = ('The old `<invoke>wrong()</invoke>` was mistakenly included in the code.\n' +
                    implementation().replace('\n```\n<invoke>', '\n<invoke>'))
        result, receipt, artifacts, _ = self.evolve([
            plan(), implementation(broken), judgment('Amend'), repaired, judgment()])
        self.assertEqual(result.status, 'completed', result.error)
        executions = [e for e in receipt['events'] if e['kind'] == 'execution']
        self.assertIn('SyntaxError', executions[0]['result']['error'])
        self.assertIsNone(executions[1]['result']['error'])
        self.assertEqual(executions[1]['upstream_example_invoke'], "source_summary('sample.py', workspace=workspace)")
        self.assertEqual(decode_tools(artifacts.get(result.candidates[0].artifact_updates['tool'])).generated_tools[
            'source_summary'].source.strip(), SOURCE.strip())
        self.assertEqual([c['purpose'] for c in receipt['calls']], ['planning', 'construction', 'judgment', 'repair', 'judgment'])

    def test_delivery_cannot_depend_on_files_created_by_an_internal_helper(self):
        helper = "def source_summary(path, workspace):\n    from pathlib import Path\n    Path(workspace, 'construction-only.json').write_text('{}')\n    return {'prepared': True}"
        dependent = "def source_summary(path, workspace):\n    from pathlib import Path\n    return {'data': Path(workspace, 'construction-only.json').read_text()}"
        graph = {"prepare": node("Prepare construction data"),
                 "source_summary": node("Final tool", ["prepare"], deliverable=True)}
        result, receipt, _, _ = self.evolve([native_plan(graph), implementation(helper), judgment(),
                                           implementation(dependent), judgment()], max_generated_tools=1)
        self.assertEqual(result.status, "empty", result.error)
        self.assertFalse(result.candidates)
        self.assertEqual(result.usage.model_calls, 5)
        self.assertEqual(result.usage.tool_calls, 3)
        check = next(e for e in receipt["events"] if e["kind"] == "delivery_check")
        self.assertFalse(check["passed"])
        self.assertIn("construction-only.json", check["result"]["error"])
        self.assertEqual(receipt["events"][-1], {"kind": "export_verified", "tools": []})

    def test_empty_plan_and_repair_limit_produce_no_candidate(self):
        cases = [([native_plan({})], {}),
                 ([plan(), implementation(), judgment("Amend")], {"max_repair_iterations": 0})]
        for replies, settings in cases:
            with self.subTest(settings=settings, replies=len(replies)):
                result, receipt, _, _ = self.evolve(replies, **settings)
                self.assertEqual(result.status, "empty", result.error)
                self.assertFalse(result.candidates)

    def test_low_score_completed_tool_can_pass_real_clean_workspace_delivery(self):
        result, receipt, _, _ = self.evolve([plan(), implementation(), judgment(score=7)])
        self.assertEqual(result.status, "completed", result.error)
        self.assertFalse(any(e["kind"] == "stored" for e in receipt["events"]))
        self.assertTrue(next(e for e in receipt["events"] if e["kind"] == "delivery_check")["passed"])

    def test_budget_failure_and_replan_retain_usage(self):
        result, receipt, _, _ = self.evolve([plan()], max_model_calls=1)
        self.assertEqual(result.status, "budget_interrupted")
        self.assertEqual(result.usage.model_calls, 1)
        result, receipt, _, _ = self.evolve([plan(), implementation(), judgment("Replan"),
            native_plan({"investigate": node("Diagnose the missing precondition", kind="QA")}),
            "Need to inspect the task-specific behavior", implementation(), judgment()])
        self.assertEqual(result.status, "completed", result.error)
        self.assertEqual(result.usage.model_calls, 7)
        self.assertEqual(sum(item["kind"] == "plan" for item in receipt["events"]), 1)
        self.assertEqual(sum(item["kind"] == "replan" for item in receipt["events"]), 1)

    def test_invalid_export_example_never_triggers_native_code_repair(self):
        invalid = implementation().replace('"example_arguments": {"path": "sample.py"}', '"example_arguments": {}')
        result, receipt, _, _ = self.evolve([plan(), invalid, judgment()])
        self.assertEqual(result.status, "empty", result.error)
        self.assertEqual(result.usage.model_calls, 3)
        self.assertEqual(result.usage.tool_calls, 1)
        self.assertIn("export_failed", [item["kind"] for item in receipt["events"]])
