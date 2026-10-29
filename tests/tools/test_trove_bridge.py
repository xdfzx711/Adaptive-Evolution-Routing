from contextlib import asynccontextmanager
import json
from pathlib import Path
import tempfile
import tomllib
import unittest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from plugins.agents.tool_selection import CodexToolAdapter
from plugins.evolution.trove.library import ToolLibrary
from plugins.tools.catalog import ToolingConfig
from tests.tools.helpers import LOCAL_CATALOG
from tests.tools.test_trove_library import path_tool


@asynccontextmanager
async def connected_library(root, library, config):
    active = CodexToolAdapter().prepare(library, config, root, Path.cwd())
    fields = tomllib.loads(active.overrides[0])["mcp_servers"]["aer_tools"]
    try:
        async with stdio_client(StdioServerParameters(command=fields["command"], args=fields["args"])) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                yield session, active
    finally:
        active.close()


class TroveBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_generated_and_fixed_tools_share_existing_bridge_and_receipts(self):
        tool = path_tool()
        library = ToolLibrary(tools=[tool], enabled_tools=[tool.name, "python_syntax_check"])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "sample.py").write_text("answer = 1\n", encoding="utf-8")
            async with connected_library(root, library, ToolingConfig(catalog=LOCAL_CATALOG, generated_python=True)) as (session, active):
                self.assertEqual([tool.name for tool in (await session.list_tools()).tools], ["path_info", "python_syntax_check"])
                result = await session.call_tool("path_info", {"path": "sample.py"})
                self.assertFalse(result.isError, result)
                self.assertEqual(json.loads(result.content[0].text), {"is_python": True})
                self.assertFalse((await session.call_tool("python_syntax_check", {"path": "sample.py"})).isError)
                self.assertTrue((await session.call_tool("path_info", {})).isError)
                self.assertEqual(active.report()["invocation_count"], 3)
                self.assertEqual(active.report()["invocations"][-1]["status"], "error")

    async def test_generated_tools_require_explicit_runtime_opt_in(self):
        tool = path_tool()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(Exception):
                async with connected_library(Path(directory), ToolLibrary(tools=[tool], enabled_tools=[tool.name]),
                    ToolingConfig(catalog={})):
                    self.fail("Disabled generated tooling must fail before discovery")
