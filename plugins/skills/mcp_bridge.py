"""Read-only skill MCP transport. The service receives one frozen library only."""

import argparse
import asyncio
import json
from pathlib import Path
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from plugins.skills.library import decode_library
from plugins.skills.runtime import SkillRuntime, READ_SPEC, READ_TOOL


async def serve(runtime, report_path):
    server = Server("aer_skills", version="1.0.0")
    report = {"status": "ready", "discovered": False, "reads": runtime.reads}

    def save():
        pending = report_path.with_suffix(".pending")
        pending.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        pending.replace(report_path)

    @server.list_tools()
    async def list_tools():
        report["discovered"] = True
        save()
        return [types.Tool(name=READ_TOOL, description=READ_SPEC.description,
            inputSchema=READ_SPEC.model_dump(mode="json")["parameters"],
            annotations=types.ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False))]

    @server.call_tool(validate_input=False)
    async def call_tool(name, arguments):
        try:
            if name != READ_TOOL or not isinstance(arguments, dict) or set(arguments) != {"name"} or not isinstance(arguments["name"], str):
                raise ValueError("read_skill requires exactly one string argument: name")
            # JSON-RPC identity is scoped to this connection, independent of CLI item IDs.
            call_id = str(server.request_context.request_id)
            output = runtime.read_skill(arguments["name"], call_id=call_id)
            result = types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(output, ensure_ascii=False))],
                                          structuredContent=output)
        except ValueError as failure:
            result = types.CallToolResult(isError=True, content=[types.TextContent(type="text", text=str(failure))])
        save()
        return result

    save()
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--library", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    data = json.loads(args.library.read_text(encoding="utf-8"))
    runtime = SkillRuntime(decode_library(json.dumps(data.pop("library"))), **data)
    asyncio.run(serve(runtime, args.report))


if __name__ == "__main__":
    main()
