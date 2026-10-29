import unittest
from unittest.mock import patch

from plugins.evolution.trove.library import PythonTool, ToolLibrary, read_library, trim_library
from plugins.evolution.trove.runtime import call_tool, run_python


def path_tool(name="path_info", **updates):
    value = {"name": name, "description": "Describe a Python path without accessing the filesystem",
        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}},
            "required": ["path"], "additionalProperties": False},
        "source": f"def {name}(path):\n return {{'is_python': path.endswith('.py')}}",
        "examples": [{"arguments": {"path": "example.py"}, "expected": {"is_python": True}}]}
    return PythonTool.model_validate({**value, **updates})


class TroveLibraryTests(unittest.TestCase):
    def test_legacy_selection_empty_library_and_roundtrip(self):
        self.assertEqual(read_library(b'{"enabled_tools":[]}'), ToolLibrary())
        tool = path_tool()
        library = ToolLibrary(tools=[tool], enabled_tools=[tool.name])
        self.assertEqual(read_library(library.model_dump_json().encode()), library)
        library.validate_catalog({})
        with self.assertRaises(ValueError):
            library.validate_catalog({tool.name: object()})
        with self.assertRaises(ValueError):
            ToolLibrary(enabled_tools=["missing"]).validate_catalog({})

    def test_dependency_order_missing_cycle_and_undeclared_calls(self):
        base = path_tool()
        child = path_tool("describe", source="def describe(path):\n return path_info(path)", dependencies=[base.name])
        library = ToolLibrary(tools=[child, base], enabled_tools=[child.name])
        self.assertEqual([tool.name for tool in library.ordered_tools()], [base.name, child.name])
        for tools in ([child], [child, base.replace(dependencies=[child.name])], [base, base]):
            with self.subTest(tools=tools), self.assertRaises(ValueError):
                ToolLibrary(tools=tools)
        with self.assertRaisesRegex(ValueError, "declared dependencies"):
            ToolLibrary(tools=[base, child.replace(dependencies=[])])

    def test_external_schema_references_are_rejected_before_validation(self):
        with self.assertRaisesRegex(ValueError, "local definitions"):
            path_tool(input_schema={"type": "object", "$ref": "https://example.com/schema"})

    def test_frequency_trimming_keeps_required_dependencies_and_configured_tools(self):
        base, unused = path_tool(), path_tool("unused")
        child = path_tool("describe", source="def describe(path):\n return path_info(path)", dependencies=[base.name])
        library = ToolLibrary(tools=[base, unused, child], enabled_tools=["fixed", child.name, unused.name],
            usage_counts={unused.name: 0, base.name: 3, child.name: 2})
        trimmed = trim_library(library, {child.name}, 2)
        self.assertEqual({tool.name for tool in trimmed.tools}, {base.name, child.name})
        self.assertEqual(trimmed.enabled_tools, (child.name, "fixed"))
        with self.assertRaises(ValueError):
            trim_library(library, {child.name}, 1)

    def test_imports_private_runtime_access_and_invalid_definitions_are_rejected(self):
        for source in ("def path_info(path):\n import os\n return {}",
                       "def path_info(path):\n return path.__class__",
                       "x = 1\ndef path_info(path):\n return {}",
                       "@str\ndef path_info(path):\n return {}",
                       "def wrong(path):\n return {}"):
            with self.subTest(source=source), self.assertRaises((ValueError, SyntaxError)):
                path_tool(source=source)


class TroveRuntimeTests(unittest.TestCase):
    def test_invalid_json_payload_never_starts_a_worker(self):
        tool = path_tool()
        with patch("plugins.evolution.trove.runtime.subprocess.Popen") as spawn:
            result = run_python(ToolLibrary(tools=[tool]), timeout=5, name=tool.name,
                arguments={"path": float("nan")})
        self.assertFalse(result["ok"])
        spawn.assert_not_called()

    def test_actual_function_dependency_and_solution_execution(self):
        base = path_tool()
        child = path_tool("describe", source="def describe(path):\n return path_info(path)", dependencies=[base.name])
        library = ToolLibrary(tools=[child, base], enabled_tools=[child.name])
        result = call_tool(library, child.name, {"path": "sample.py"}, 5)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["output"], {"is_python": True})
        self.assertEqual(result["used_tools"], [child.name, base.name])
        solved = run_python(library, timeout=5, solution="def solve():\n return describe(task['path'])", task={"path": "x.txt"})
        self.assertEqual(solved["output"], {"is_python": False})

    def test_standard_library_and_json_only_output(self):
        tool = path_tool(source="def path_info(path):\n import re\n return {'is_python': bool(re.search(r'\\.py$', path))}")
        result = call_tool(ToolLibrary(tools=[tool]), tool.name, {"path": "x.py"}, 5)
        self.assertTrue(result["ok"], result)
        for source in ("def path_info(path):\n return {1}", "def path_info(path):\n return {'value': float('nan')}",
                       "def path_info(path):\n return {'value': open(path).read()}"):
            with self.subTest(source=source):
                bad = path_tool(source=source)
                self.assertFalse(call_tool(ToolLibrary(tools=[bad]), bad.name, {"path": "x.py"}, 5)["ok"])

    def test_timeout_terminates_worker_and_invalid_arguments_never_execute(self):
        tool = path_tool(source="def path_info(path):\n while True:\n  pass")
        result = call_tool(ToolLibrary(tools=[tool]), tool.name, {"path": "x"}, 0.6)
        self.assertEqual(result["error"], "timeout")
        with self.assertRaises(Exception):
            call_tool(ToolLibrary(tools=[tool]), tool.name, {}, 5)
