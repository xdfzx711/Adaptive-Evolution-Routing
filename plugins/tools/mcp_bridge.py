"""Per-execution STDIO MCP bridge. Stdout is reserved for MCP messages."""

import argparse
import asyncio
from contextlib import AsyncExitStack
from datetime import timedelta
import json
import os
from pathlib import Path
import sys
from time import monotonic

# Codex launches this absolute script from an isolated task working directory.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import httpx
import jsonschema
from dotenv import dotenv_values
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from plugins.tool_selection import ToolSelection
from plugins.tools.catalog import ToolingConfig
from plugins.tools.local import local_tool_registry
from plugins.tools.generated.bundle import decode_tools, generated_tools, validate_tools
from plugins.tools.generated.runtime import execute_generated
from plugins.tools.generated.contracts import classify_result
from plugins.tools.invocation_receipts import begin, finish, digest, empty_counts


class ToolSetupError(ValueError):
    pass


def save_report(path, value):
    pending = path.with_suffix(".pending")
    pending.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    pending.replace(path)


def credential(name, dotenv):
    value = os.environ.get(name, dotenv.get(name))
    if not value:
        raise ToolSetupError("missing_credential")
    return value


async def serve(config, selection, workspace, report_path, report):
    registry, exposed, upstreams = local_tool_registry(), {}, {}
    dotenv = dotenv_values(config.dotenv_path, interpolate=False) if config.dotenv_path else {}
    server = Server("aer_tools", version="1.0.0",
        instructions="Tools are bound to this task's working directory. Use paths relative to that directory.")

    async def roots(_):
        return types.ListRootsResult(roots=[types.Root(uri=workspace.as_uri(), name="task-workspace")])

    async with AsyncExitStack() as stack:
        null = stack.enter_context(open(os.devnull, "w"))
        for name in selection.enabled_tools:
            if name in generated_tools(selection):
                descriptor = generated_tools(selection)[name].descriptor()
                exposed[name] = types.Tool(name=name, description=descriptor.description,
                    inputSchema=descriptor.model_dump(mode="json")["input_schema"])
                continue
            entry = config.catalog[name]
            if entry.kind == "local":
                descriptor = entry.public_descriptor()
                exposed[name] = types.Tool(name=name, description=descriptor.description,
                    inputSchema=descriptor.model_dump(mode="json")["input_schema"],
                    annotations=types.ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False))
                continue
            if entry.server not in upstreams:
                connection = config.servers[entry.server]
                async with asyncio.timeout(config.startup_timeout_seconds):
                    if connection.transport == "stdio":
                        command = [value.replace("{workspace}", str(workspace)) for value in connection.command]
                        params = StdioServerParameters(command=command[0], args=command[1:], cwd=workspace,
                            env={key: credential(key, dotenv) for key in connection.env_vars})
                        read, write = await stack.enter_async_context(stdio_client(params, errlog=null))
                    else:
                        headers = {key: credential(env, dotenv) for key, env in connection.header_envs.items()}
                        if connection.bearer_token_env:
                            headers["Authorization"] = "Bearer " + credential(connection.bearer_token_env, dotenv)
                        client = await stack.enter_async_context(httpx.AsyncClient(headers=headers,
                            timeout=httpx.Timeout(config.tool_timeout_seconds, connect=config.startup_timeout_seconds)))
                        read, write, _ = await stack.enter_async_context(streamable_http_client(connection.url, http_client=client))
                    session = await stack.enter_async_context(ClientSession(read, write,
                        read_timeout_seconds=timedelta(seconds=max(config.startup_timeout_seconds, config.tool_timeout_seconds)),
                        list_roots_callback=roots))
                    initialized = await session.initialize()
                    if initialized.serverInfo.version != connection.version:
                        raise ToolSetupError("server_version_mismatch")
                    definitions, cursor, seen = {}, None, set()
                    while True:
                        page = await session.list_tools(cursor=cursor)
                        for tool in page.tools:
                            if tool.name in definitions:
                                raise ToolSetupError("duplicate_upstream_tool")
                            definitions[tool.name] = tool
                        cursor = page.nextCursor
                        if cursor is None:
                            break
                        if cursor in seen:
                            raise ToolSetupError("invalid_tool_pagination")
                        seen.add(cursor)
                    upstreams[entry.server] = session, definitions
            _, definitions = upstreams[entry.server]
            if entry.tool_name not in definitions:
                raise ToolSetupError("selected_tool_missing")
            original = definitions[entry.tool_name]
            if entry.input_schema is not None and entry.input_schema != original.inputSchema:
                raise ToolSetupError("tool_schema_mismatch")
            exposed[name] = original.model_copy(update={"name": name, "description": entry.description})

        report.update(status="ready", available_tools=sorted(exposed))
        report["tool_metrics"] = {name: empty_counts("generated" if name in generated_tools(selection) else
            config.catalog[name].kind) for name in exposed}
        save_report(report_path, report)

        @server.list_tools()
        async def list_tools():
            report["discovered"] = True
            save_report(report_path, report)
            return list(exposed.values())

        @server.call_tool(validate_input=False)
        async def call_tool(name, arguments):
            started = monotonic()
            generated = generated_tools(selection).get(name)
            kind = "generated" if generated is not None else config.catalog[name].kind if name in config.catalog else "unknown"
            invocation = begin(report, name, kind, arguments, digest(generated.model_dump(mode="json")) if generated else None)
            save_report(report_path, report)
            outcome, generated_output = None, None
            try:
                if name not in exposed:
                    raise ValueError("Tool is not selected")
                # Validate here so invalid arguments also receive an invocation receipt.
                jsonschema.validate(arguments, exposed[name].inputSchema)
                async with asyncio.timeout(config.tool_timeout_seconds):
                    if name in generated_tools(selection):
                        output = await asyncio.to_thread(execute_generated, generated_tools(selection)[name], arguments,
                            workspace, timeout_seconds=max(0.1, config.tool_timeout_seconds - 1),
                            python_executable=config.generated_tools.python_executable)
                        generated_output = output
                        outcome = classify_result(generated, output)
                        result = types.CallToolResult(isError=output["status"] == "error",
                            content=[types.TextContent(type="text", text=json.dumps(output, ensure_ascii=False))],
                            structuredContent=output)
                    elif config.catalog[name].kind == "local":
                        entry = config.catalog[name]
                        output = await asyncio.to_thread(registry.get(entry.plugin).invoke, workspace, arguments)
                        result = types.CallToolResult(content=[types.TextContent(type="text",
                            text=json.dumps(output, ensure_ascii=False))], structuredContent=output)
                    else:
                        entry = config.catalog[name]
                        result = await upstreams[entry.server][0].call_tool(entry.tool_name, arguments=arguments)
            except Exception as failure:
                # Never copy remote exceptions, headers or credential values into task feedback.
                message = (str(failure) if name in config.catalog and config.catalog[name].kind == "local"
                           and isinstance(failure, (ValueError, SyntaxError)) else "Tool call failed: " + type(failure).__name__)
                result = types.CallToolResult(isError=True, content=[types.TextContent(type="text", text=message)])
            finish(report, invocation, error=result.isError, elapsed=monotonic() - started,
                outcome=outcome, result=generated_output)
            save_report(report_path, report)
            return result

        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    report = {"status": "starting", "available_tools": [], "discovered": False,
              "invocation_count": 0, "invocations": [], "error": None}
    try:
        data = json.loads(args.config.read_text(encoding="utf-8"))
        config = ToolingConfig.model_validate(data["tooling"])
        selection = decode_tools(json.dumps(data["selection"]))
        validate_tools(selection, config.catalog,
            allow_generated=bool(config.generated_tools and config.generated_tools.enabled))
        workspace = args.workspace.resolve(strict=True)
        save_report(args.report, report)
        asyncio.run(serve(config, selection, workspace, args.report, report))
    except Exception as failure:
        # ExceptionGroup wrappers must not reveal upstream credentials either.
        pending = [failure]
        reason = type(failure).__name__
        while pending:
            error = pending.pop()
            if isinstance(error, ToolSetupError):
                reason = str(error)
                break
            pending.extend(getattr(error, "exceptions", ()))
        report.update(status="error", error=reason)
        save_report(args.report, report)
        print("AER tool bridge failed: " + reason, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
