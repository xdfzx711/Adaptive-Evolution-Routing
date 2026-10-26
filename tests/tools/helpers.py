from contextlib import asynccontextmanager
from pathlib import Path
import tomllib

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from plugins.agents.tool_selection import CodexToolAdapter
from plugins.tool_selection import ToolSelection


LOCAL_CATALOG = {name: {"kind": "local", "plugin": name}
                 for name in ("python_structure", "python_syntax_check")}


@asynccontextmanager
async def connected_bridge(root, config, tools):
    active = CodexToolAdapter().prepare(ToolSelection(enabled_tools=tools), config, root, Path.cwd())
    fields = tomllib.loads(active.overrides[0])["mcp_servers"]["aer_tools"]
    try:
        async with stdio_client(StdioServerParameters(command=fields["command"], args=fields["args"])) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                yield session, active
    finally:
        active.close()
