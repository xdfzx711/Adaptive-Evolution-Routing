import importlib.util
from pathlib import Path
import tempfile
import unittest

from plugins.agents.tool_selection import CodexToolAdapter
from plugins.tool_selection import ToolSelection
from plugins.tools.catalog import ToolingConfig
from plugins.tools.generated.bundle import GeneratedTool, ToolBundle, decode_tools, validate_tools
from plugins.tools.generated.runtime import execute_generated


HAS_KERNEL = importlib.util.find_spec("ipykernel") is not None
SCHEMA = {"type": "object", "properties": {"path": {"type": "string"}},
          "required": ["path"], "additionalProperties": False}
SOURCE = '''def source_summary(path, workspace):
    from pathlib import Path
    print("Traceback is ordinary output here")
    return {"exists": (Path(workspace) / path).exists(), "cwd": str(Path.cwd()), "workspace": workspace}
'''


def tool(source=SOURCE, **values):
    return GeneratedTool(description="Inspect a file in the current task workspace", source=source,
        entrypoint="source_summary", input_schema=SCHEMA, **values)


class ToolBundleTests(unittest.TestCase):
    def test_legacy_encoding_and_self_contained_versions(self):
        old = ToolSelection(enabled_tools=["static_tool"])
        self.assertEqual(decode_tools(CodexToolAdapter().encode(old)), old)
        bundle = ToolBundle(enabled_tools=["source_summary"], generated_tools={"source_summary": tool()})
        restored = decode_tools(CodexToolAdapter().encode(bundle))
        self.assertEqual(restored, bundle)
        changed = bundle.replace(generated_tools={"source_summary": tool(SOURCE.replace(".exists()", ".is_file()"))})
        self.assertNotEqual(CodexToolAdapter().encode(bundle), CodexToolAdapter().encode(changed))
        for catalog, enabled in (({}, False), ({"source_summary": object()}, True)):
            with self.assertRaises(ValueError):
                validate_tools(bundle, catalog, allow_generated=enabled)


@unittest.skipUnless(HAS_KERNEL, "install the [oscopilot] extra")
class GeneratedRuntimeTests(unittest.TestCase):
    def test_actual_cwd_workspace_binding_and_structured_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "module.py").write_text("value = 1", encoding="utf-8")
            result = execute_generated(tool(), {"path": "module.py"}, root)
            self.assertEqual(result["status"], "completed", result)
            self.assertTrue(result["result"]["exists"])
            self.assertEqual(Path(result["result"]["cwd"]), root)
            self.assertEqual(Path(result["result"]["workspace"]), root)
            self.assertIn("Traceback", result["stdout"])
            broken = tool("def source_summary(path, workspace):\n    raise ValueError('fixture failure')")
            failure = execute_generated(broken, {"path": "module.py"}, root)
            self.assertEqual(failure["status"], "error")
            self.assertIn("fixture failure", failure["error"])

    def test_class_entrypoint_and_timeout(self):
        source = '''class Inspector:
    def __init__(self, prefix): self.prefix = prefix
    def __call__(self, path, workspace): return self.prefix + path
'''
        implementation = GeneratedTool(description="Callable class", source=source, entrypoint="Inspector",
            kind="class", constructor_arguments={"prefix": "checked:"}, input_schema=SCHEMA)
        with tempfile.TemporaryDirectory() as directory:
            result = execute_generated(implementation, {"path": "module.py"}, directory)
            self.assertEqual(result["result"], "checked:module.py", result)
            hung = tool("def source_summary(path, workspace):\n    while True: pass")
            result = execute_generated(hung, {"path": "module.py"}, directory, timeout_seconds=3)
            self.assertEqual(result["status"], "error")
            self.assertIn("timed out", result["error"])


@unittest.skipUnless(HAS_KERNEL, "install the [oscopilot] extra")
class GeneratedMCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_mcp_discovers_and_calls_exact_snapshot_implementation(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        import tomllib

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "module.py").write_text("pass", encoding="utf-8")
            bundle = ToolBundle(enabled_tools=["source_summary"], generated_tools={"source_summary": tool()})
            config = ToolingConfig(catalog={}, generated_tools={"enabled": True})
            active = CodexToolAdapter().prepare(bundle, config, root, Path.cwd())
            fields = tomllib.loads(active.overrides[0])["mcp_servers"]["aer_tools"]
            try:
                async with stdio_client(StdioServerParameters(command=fields["command"], args=fields["args"])) as streams:
                    async with ClientSession(*streams) as session:
                        await session.initialize()
                        listed = (await session.list_tools()).tools
                        self.assertEqual([item.name for item in listed], ["source_summary"])
                        self.assertEqual(listed[0].inputSchema, SCHEMA)
                        result = await session.call_tool("source_summary", {"path": "module.py"})
                        self.assertFalse(result.isError, result)
                        self.assertTrue(result.structuredContent["result"]["exists"])
                        self.assertTrue((await session.call_tool("source_summary", {"unknown": 1})).isError)
                        self.assertEqual(active.report()["invocation_count"], 2)
            finally:
                active.close()
