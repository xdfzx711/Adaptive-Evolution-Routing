"""Export must not mistake native prose for metadata or mutate active tools."""

from pathlib import Path
from dataclasses import replace
import json
import tempfile
import unittest

from plugins.evolution.oscopilot.config import OSCopilotConfig
from plugins.evolution.oscopilot.constructed import ConstructedTool
from plugins.evolution.oscopilot.exporter import export_candidates, export_metadata, specification, ToolSpecification
from plugins.evolution.oscopilot.callable_metadata import callable_inventory, MetadataError
from plugins.tools.generated.bundle import ToolBundle
from tests.evolution.test_oscopilot import implementation, node
from tests.tools.test_generated import SOURCE, tool


class NoModel:
    def scoped(self, *args, **kwargs):
        raise AssertionError("Deterministic export should need no model request")


class MetadataModel:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.requests = []

    def scoped(self, purpose, *, include_evidence=True):
        assert purpose in {"export_metadata", "export_metadata_format"}
        assert include_evidence is False
        return self

    def chat(self, messages):
        self.requests.append(messages)
        return next(self.replies)


class ExportTests(unittest.TestCase):
    def test_recorded_16263_missing_brace_metadata_becomes_candidate(self):
        fixture = json.loads((Path(__file__).parents[1] / "fixtures/oscopilot/missing_metadata_brace.json").read_text(encoding="utf-8"))
        raw = fixture["response"]
        # The controlled format-repair reply restores the missing delimiter. Production
        # asks the model; it never guesses a delimiter or changes native source locally.
        model = MetadataModel([raw, raw + "}"])
        product = ConstructedTool(fixture["tool"], fixture["source"], fixture["invoke"], fixture["description"], "", None, {})
        with tempfile.TemporaryDirectory() as root:
            candidate = export_candidates(ToolBundle(), [product], Path(root), model, OSCopilotConfig(), [])
        self.assertEqual(set(candidate.examples), {fixture["tool"]})
        self.assertEqual(candidate.bundle.generated_tools[fixture["tool"]].source, fixture["source"])
        self.assertEqual(len(model.requests), 2)

    def metadata(self):
        return {"specification": specification(implementation()).model_dump(mode="json"),
                "interface": node("Inspect", deliverable=True)["interface"]}

    def test_missing_final_brace_recovers_with_default_budget_and_small_request(self):
        metadata = self.metadata()
        raw = json.dumps(metadata)
        product = ConstructedTool("source_summary", SOURCE, "source_summary('sample.py', workspace=workspace)",
                                 "Inspect", implementation(), None, {"result": "large evidence" * 10000})
        model = MetadataModel([raw[:-1], raw])
        events = []
        result = export_metadata(product, model, OSCopilotConfig(), events)
        self.assertEqual(result.specification.entrypoint, "source_summary")
        repair = json.loads(model.requests[1][-1]["content"])
        self.assertEqual(set(repair), {"response", "parse_error", "response_schema"})
        self.assertEqual(repair["response"], raw[:-1])
        self.assertNotIn(SOURCE, model.requests[1][-1]["content"])
        self.assertNotIn("large evidence", model.requests[1][-1]["content"])
        self.assertEqual(events[-1]["attempts"], 2)

    def test_factory_failure_is_classified_without_metadata_request_or_source_rewriting(self):
        source = "def builder():\n    def analyze(values):\n        return list(values)\n    return analyze\n"
        product = ConstructedTool("builder", source, "builder()", "Build analyzer", "", None, {})
        events = []
        active = ToolBundle()
        with tempfile.TemporaryDirectory() as root:
            candidate = export_candidates(active, [product], Path(root), NoModel(), OSCopilotConfig(), events)
        self.assertEqual(candidate.bundle, active)
        failure = next(e for e in events if e["kind"] == "export_failed")
        self.assertEqual(failure["error_code"], "incompatible_callable")
        self.assertEqual(failure["source"], source)
        self.assertIn("top-level", failure["error"])

    def test_nested_entrypoint_and_invented_parameters_have_distinct_feedback(self):
        product = ConstructedTool("source_summary", SOURCE, "source_summary('sample.py', workspace=workspace)",
                                 "Inspect", implementation(), None, {})
        for mutation, code in (("nested", "metadata_entrypoint_mismatch"), ("parameter", "metadata_signature_mismatch")):
            metadata = self.metadata()
            if mutation == "nested":
                metadata["specification"]["entrypoint"] = "nested_analyzer"
            else:
                metadata["specification"]["input_schema"]["properties"]["invented"] = {"type": "string"}
            with self.subTest(mutation=mutation), self.assertRaises(MetadataError) as raised:
                export_metadata(product, MetadataModel([json.dumps(metadata)]), OSCopilotConfig(), [])
            self.assertEqual(raised.exception.code, code)

    def test_optional_keyword_only_and_workspace_parameters_remain_supported(self):
        source = "def source_summary(path, *, limit=3, workspace=None):\n    return {'exists': True}\n"
        product = ConstructedTool("source_summary", source, "source_summary('sample.py')", "Inspect",
                                 implementation(source), node("Inspect", deliverable=True)["interface"], {})
        self.assertEqual(export_metadata(product, NoModel(), OSCopilotConfig(), []).specification.entrypoint, "source_summary")
        facts = callable_inventory(source)[0]
        self.assertEqual(facts["required"], ["path"])
        self.assertEqual(facts["parameters"], ["path", "limit", "workspace"])

    def test_callable_class_constructor_and_call_are_separate(self):
        source = "class Inspector:\n    def __init__(self, prefix): self.prefix = prefix\n    def __call__(self, path, workspace): return {'exists': True}\n"
        metadata = self.metadata()
        metadata["specification"].update(entrypoint="Inspector", kind="class", constructor_arguments={"prefix": "x"})
        product = ConstructedTool("inspect", source, "Inspector('x')('sample.py', workspace)", "Inspect", "", None, {})
        result = export_metadata(product, MetadataModel([json.dumps(metadata)]), OSCopilotConfig(), [])
        self.assertEqual(result.specification.kind, "class")

    def test_nested_helper_returning_json_does_not_make_outer_tool_a_factory(self):
        source = "def outer():\n    def helper():\n        return lambda: 1\n    return {'value': 1}\n"
        self.assertFalse(callable_inventory(source)[0]["returns_callable"])

    def test_malformed_schema_shapes_fail_as_metadata_errors_before_path_adaptation(self):
        spec = specification(implementation()).model_dump(mode="json")
        for schema in ({"type": "object", "properties": ["workspace"]},
                       {"type": "object", "required": "workspace"}, {"type": "array"}):
            with self.subTest(schema=schema):
                with self.assertRaises(ValueError):
                    ToolSpecification.model_validate({**spec, "input_schema": schema})

    def test_metadata_completion_can_correct_planned_interface_to_match_completed_tool(self):
        planned = node("Inspect", deliverable=True)["interface"]
        actual = {**planned, "output_schema": {"type": "object", "required": ["exists"]}}
        metadata = {"specification": specification(implementation()).model_dump(mode="json"), "interface": actual}
        # A class-style invocation cannot be inferred: metadata must describe its actual entrypoint.
        product = ConstructedTool("source_summary", SOURCE, "factory()('sample.py')", "Inspect", "", planned, {})
        model = MetadataModel([json.dumps(metadata)])
        result = export_metadata(product, model, OSCopilotConfig(), [])
        self.assertEqual(result.interface.model_dump(mode="json")["output_schema"], actual["output_schema"])
        self.assertEqual(len(model.requests), 1)

    def test_malformed_metadata_json_uses_bounded_metadata_recovery(self):
        metadata = {"specification": specification(implementation()).model_dump(mode="json"),
                    "interface": node("Inspect", deliverable=True)["interface"]}
        product = ConstructedTool("source_summary", SOURCE, "source_summary('sample.py', workspace=workspace)",
                                 "Inspect", implementation(), None, {})
        model = MetadataModel(['not JSON', json.dumps(metadata)])
        events = []
        result = export_metadata(product, model, OSCopilotConfig(max_export_metadata_repairs=2), events)
        self.assertEqual(result.specification.entrypoint, "source_summary")
        self.assertEqual(len(model.requests), 2)
        self.assertIn('not JSON', model.requests[1][-1]["content"])
        self.assertEqual(events[-1]["attempts"], 2)

    def test_exhausted_metadata_recovery_is_an_empty_candidate_not_an_evolution_exception(self):
        product = ConstructedTool("source_summary", SOURCE, "source_summary('sample.py', workspace=workspace)",
                                 "Inspect", implementation(), None, {})
        active = ToolBundle(enabled_tools=[], generated_tools={})
        model = MetadataModel(['not JSON', 'still not JSON'])
        with tempfile.TemporaryDirectory() as directory:
            events = []
            candidate = export_candidates(active, (product,), Path(directory), model,
                                          OSCopilotConfig(max_export_metadata_repairs=2), events)
        self.assertEqual(candidate.bundle, active)
        self.assertEqual(len(model.requests), 2)
        self.assertTrue(any(e["kind"] == "export_failed" for e in events))

    def test_prose_mentions_and_bare_json_do_not_break_metadata_extraction(self):
        original = implementation()
        expected = specification(original)
        for content in (
            'Error analysis: the original code lacked `<tool_spec>` metadata.\n' + original,
            original.replace('<tool_spec>', '').replace('</tool_spec>', ''),
            'Mention `<tool_spec>` twice: `<tool_spec>`.\n' + original,
        ):
            with self.subTest(content=content[:80]):
                self.assertEqual(specification(content), expected)

    def test_multiple_specifications_are_not_silently_selected(self):
        with self.assertRaisesRegex(ValueError, "unambiguous"):
            specification(implementation() + '\n<tool_spec>' + specification(implementation()).model_dump_json() + '</tool_spec>')

    def test_description_paths_do_not_block_working_code_or_modify_the_active_bundle(self):
        active = ToolBundle(enabled_tools=["source_summary"], generated_tools={"source_summary": tool()})
        before = active.model_dump_json()
        interface = node("Inspect", deliverable=True)["interface"]
        changed = SOURCE.replace('.exists()', '.is_file()')
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            product = ConstructedTool("source_summary", changed, "source_summary('sample.py', workspace=workspace)",
                f"Inspect files in '{workspace}'", implementation(changed).split('<tool_spec>')[0], interface,
                {"status": "completed", "result": {"exists": True}, "error": None, "stdout": ""})
            events = []
            candidate = export_candidates(active, (product,), workspace, NoModel(), OSCopilotConfig(), events)
        self.assertEqual(active.model_dump_json(), before)
        self.assertNotEqual(candidate.bundle, active)
        self.assertEqual(candidate.bundle.generated_tools["source_summary"].source, changed)
        self.assertEqual(candidate.bundle.generated_tools["source_summary"].description, "Inspect files in '<workspace>'")
        self.assertEqual(events[-1]["state"], "candidate")
        self.assertEqual(candidate.examples, {"source_summary": {"path": "sample.py"}})

    def test_one_failed_export_preserves_stable_implementation_and_keeps_other_candidate(self):
        active = ToolBundle(enabled_tools=["source_summary"], generated_tools={"source_summary": tool()})
        interface = node("Inspect", deliverable=True)["interface"]
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            source = "def source_summary(path, workspace):\n    return " + repr(str(workspace / "data"))
            bad = ConstructedTool("source_summary", source, "source_summary('sample.py', workspace=workspace)",
                "Bad source", implementation(source), interface, {})
            good = ConstructedTool("second", SOURCE, "source_summary('sample.py', workspace=workspace)",
                "Inspect", implementation(), interface, {})
            events = []
            candidate = export_candidates(active, (bad, good), workspace, NoModel(), OSCopilotConfig(max_generated_tools=1), events)
        self.assertEqual(candidate.bundle.generated_tools["source_summary"], active.generated_tools["source_summary"])
        self.assertEqual(set(candidate.examples), {"second"})
        self.assertEqual(set(active.generated_tools), {"source_summary"})
        self.assertTrue(any(e["kind"] == "export_failed" and e["tool"] == "source_summary" for e in events))

    def test_explicit_export_cap_preserves_existing_active_tools(self):
        active = ToolBundle(enabled_tools=["source_summary"], generated_tools={"source_summary": tool()})
        product = ConstructedTool("first", SOURCE, "source_summary('sample.py', workspace=workspace)",
                                 "Inspect", implementation(), node("Inspect", deliverable=True)["interface"], {})
        with tempfile.TemporaryDirectory() as directory:
            events = []
            candidate = export_candidates(active, (product, replace(product, name="second")), Path(directory),
                                          NoModel(), OSCopilotConfig(max_generated_tools=1), events)
        self.assertEqual(set(candidate.bundle.generated_tools), {"source_summary", "first"})
        self.assertEqual(set(active.generated_tools), {"source_summary"})
        self.assertEqual(candidate.bundle.generated_tools["source_summary"], active.generated_tools["source_summary"])
        self.assertEqual(set(candidate.examples), {"first"})
