from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from plugins.agents.tool_selection import CodexToolAdapter
from plugins.tool_selection import ToolSelection
from plugins.tools.catalog import ToolingConfig
from tests.tools.helpers import LOCAL_CATALOG, connected_bridge


UPSTREAM = Path(__file__).with_name("upstream_fixture.py")


def remote_config(*, transport="stdio", url=None, version="fixture-1", name="lookup", **extra):
    connection = {"transport": transport, "version": version,
        **({"command": [sys.executable, str(UPSTREAM)]} if transport == "stdio" else {"url": url})}
    return ToolingConfig.model_validate({"catalog": {
        "selected_remote": {"kind": "mcp", "server": "fixture", "tool_name": name,
            "description": "Look up a query", "version": version}},
        "servers": {"fixture": connection}, **extra})


@contextmanager
def http_service():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.close()
    process = subprocess.Popen([sys.executable, str(UPSTREAM), "--port", str(port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}))
    try:
        deadline = time.monotonic() + 15
        while True:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                if time.monotonic() >= deadline or process.poll() is not None:
                    raise RuntimeError("HTTP fixture did not start")
                time.sleep(0.05)
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        process.terminate()
        process.wait(timeout=10)


class MCPBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_tools_are_discovered_called_and_cannot_read_other_workspaces(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "workspace"
            root.mkdir()
            (root / "module.py").write_text("class Demo:\n pass\n", encoding="utf-8")
            (root.parent / "private.py").write_text("answer = 42", encoding="utf-8")
            async with connected_bridge(root, ToolingConfig(catalog=LOCAL_CATALOG), ["python_structure"]) as (session, active):
                self.assertEqual([tool.name for tool in (await session.list_tools()).tools], ["python_structure"])
                result = await session.call_tool("python_structure", {"path": "module.py"})
                self.assertFalse(result.isError)
                self.assertEqual(json.loads(result.content[0].text)["definitions"][0]["name"], "Demo")
                escaped = await session.call_tool("python_structure", {"path": "../private.py"})
                self.assertTrue(escaped.isError)
                self.assertNotIn("42", escaped.content[0].text)
                self.assertTrue((await session.call_tool("python_syntax_check", {"path": "module.py"})).isError)
                self.assertTrue((await session.call_tool("python_structure", {})).isError)
                report = active.report()
                self.assertEqual(report["status"], "ready")
                self.assertTrue(report["discovered"])
                self.assertEqual(report["invocation_count"], 4)
                self.assertEqual([item["status"] for item in report["invocations"]], ["completed", "error", "error", "error"])
                temporary_root = Path(active.temporary.name)
            self.assertFalse(temporary_root.exists())

    async def test_stdio_upstream_selection_and_dotenv_credentials_are_applied(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = root / "fixture.env"
            env.write_text("AER_TEST_TOOL_SECRET=fixture-credential-value\n", encoding="utf-8")
            cfg = remote_config(dotenv_path=str(env))
            cfg = cfg.model_copy(update={"servers": {"fixture": cfg.servers["fixture"].model_copy(update={"env_vars": ["AER_TEST_TOOL_SECRET"]})}})
            async with connected_bridge(root, cfg, ["selected_remote"]) as (session, active):
                self.assertEqual([tool.name for tool in (await session.list_tools()).tools], ["selected_remote"])
                result = await session.call_tool("selected_remote", {"query": "public question"})
                self.assertFalse(result.isError)
                data = json.loads(result.content[0].text)
                self.assertTrue(data["credential_present"])
                self.assertEqual(Path(data["workspace"]), root)
                self.assertTrue((await session.call_tool("hidden_tool", {})).isError)
                self.assertNotIn("fixture-credential-value", json.dumps(active.report()))

    async def test_streamable_http_transport_and_tool_call_timeout(self):
        with tempfile.TemporaryDirectory() as directory, http_service() as url:
            root = Path(directory)
            async with connected_bridge(root, remote_config(transport="streamable_http", url=url), ["selected_remote"]) as (session, _):
                await session.list_tools()
                result = await session.call_tool("selected_remote", {"query": "HTTP lookup"})
                self.assertFalse(result.isError)
                self.assertEqual(json.loads(result.content[0].text)["query"], "HTTP lookup")
            async with connected_bridge(root, remote_config(name="delayed", tool_timeout_seconds=0.1), ["selected_remote"]) as (session, active):
                await session.list_tools()
                result = await session.call_tool("selected_remote", {"query": "timeout"})
                self.assertTrue(result.isError)
                self.assertEqual(active.report()["invocations"][-1]["status"], "error")

    async def test_startup_failures_record_missing_tool_version_and_credentials_without_secrets(self):
        cases = [(remote_config(version="wrong"), "server_version_mismatch"),
                 (remote_config(name="missing"), "selected_tool_missing")]
        credentials = remote_config()
        credentials = credentials.model_copy(update={"servers": {"fixture": credentials.servers["fixture"].model_copy(
            update={"env_vars": ["AER_TEST_MISSING_TOOL_KEY"]})}})
        cases.append((credentials, "missing_credential"))
        for cfg, reason in cases:
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as directory:
                active = CodexToolAdapter().prepare(ToolSelection(enabled_tools=["selected_remote"]), cfg, Path(directory), Path.cwd())
                fields = tomllib.loads(active.overrides[0])["mcp_servers"]["aer_tools"]
                try:
                    with open(os.devnull, "w") as null:
                        with self.assertRaises(Exception):
                            async with stdio_client(StdioServerParameters(command=fields["command"], args=fields["args"]), errlog=null) as streams:
                                async with ClientSession(*streams) as session:
                                    await session.initialize()
                    report = active.report()
                    self.assertEqual(report["status"], "error")
                    self.assertEqual(report["error"], reason)
                finally:
                    active.close()
