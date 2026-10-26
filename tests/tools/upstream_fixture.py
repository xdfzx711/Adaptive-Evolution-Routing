"""Local MCP fixture for STDIO, HTTP, credentials, filtering and timeouts."""

import argparse
import asyncio
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int)
    parser.add_argument("--startup-delay", type=float, default=0)
    args = parser.parse_args()
    if args.startup_delay:
        import time
        time.sleep(args.startup_delay)
    service = FastMCP("upstream-fixture", host="127.0.0.1", port=args.port or 8000,
        json_response=True, stateless_http=True, log_level="ERROR")
    service._mcp_server.version = "fixture-1"

    @service.tool()
    def lookup(query: str) -> dict:
        return {"query": query, "workspace": str(Path.cwd()),
                "credential_present": bool(os.environ.get("AER_TEST_TOOL_SECRET"))}

    @service.tool()
    async def delayed(query: str) -> dict:
        await asyncio.sleep(10)
        return {"query": query}

    @service.tool()
    def hidden_tool() -> dict:
        return {"unselected": True}

    service.run(transport="streamable-http" if args.port else "stdio")


if __name__ == "__main__":
    main()
