"""Offline CLI fixture: reads public tasks only and emits Codex-shaped events."""

import ast
import json
import operator
from pathlib import Path
import re
import sys
import time
import tomllib


def emit(value):
    print(json.dumps(value), flush=True)


def calculate(node):
    if isinstance(node, ast.Expression):
        return calculate(node.body)
    if isinstance(node, ast.Constant) and type(node.value) in {int, float}:
        return node.value
    functions = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
                 ast.Div: operator.truediv, ast.USub: operator.neg, ast.UAdd: operator.pos}
    if isinstance(node, ast.BinOp) and type(node.op) in functions:
        return functions[type(node.op)](calculate(node.left), calculate(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in functions:
        return functions[type(node.op)](calculate(node.operand))
    raise ValueError("unsupported fixture expression")


async def exercise_tools(config, workspace, *, call=True):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    selected = next((tomllib.loads(value)["mcp_servers"]["aer_tools"] for value in config
                     if value.startswith("mcp_servers.aer_tools=")), None)
    if selected is None:
        return []
    (workspace / "analysis_sample.py").write_text("def example():\n return 1\n", encoding="utf-8")
    async with stdio_client(StdioServerParameters(command=selected["command"], args=selected["args"])) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            if call:
                for index, tool in enumerate(tools):
                    arguments = {"path": "analysis_sample.py"}
                    result = await session.call_tool(tool.name, arguments)
                    emit({"type": "item.completed", "item": {"id": f"mcp-{index}", "type": "mcp_tool_call",
                        "server": "aer_tools", "tool": tool.name, "arguments": arguments,
                        "result": result.model_dump(mode="json"), "status": "failed" if result.isError else "completed"}})
            return [tool.name for tool in tools]


async def exercise_skills(config, *, call=True, name="test-skill"):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    selected = next(tomllib.loads(value)["mcp_servers"]["aer_skills"] for value in config
                    if value.startswith("mcp_servers.aer_skills="))
    async with stdio_client(StdioServerParameters(command=selected["command"], args=selected["args"])) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            await session.list_tools()
            if call:
                result = await session.call_tool("read_skill", {"name": name})
                emit({"type": "item.completed", "item": {"id": "skill-1", "type": "mcp_tool_call",
                    "server": "aer_skills", "tool": "read_skill", "arguments": {"name": name},
                    "result": result.model_dump(mode="json"), "status": "failed" if result.isError else "completed"}})
                return result.structuredContent


def main():
    if "--version" in sys.argv:
        print("fake-codex 0.162.1")
        return 0
    mode = sys.argv[sys.argv.index("--fixture-mode") + 1] if "--fixture-mode" in sys.argv else "model"
    if mode.startswith("startup-"):
        state = Path(sys.argv[sys.argv.index("--fixture-state-file") + 1])
        count = int(state.read_text()) + 1 if state.is_file() else 1
        state.write_text(str(count))
        if mode != "startup-once" or count == 1:
            if mode == "startup-after-thread":
                emit({"type": "thread.started", "thread_id": "already-started"})
            print("failed to load AGENTS.md: CreateProcessSecurityEnvironment failed (HRESULT = 0x80070003)", file=sys.stderr)
            return 1
    if mode == "timeout":
        time.sleep(60)
    if mode == "timeout-after-thread":
        emit({"type": "thread.started", "thread_id": "already-started"})
        emit({"type": "turn.started"})
        print("fixture model connection timed out", file=sys.stderr, flush=True)
        time.sleep(60)
    task = json.load(sys.stdin)
    model = sys.argv[sys.argv.index("--model") + 1]
    config = [sys.argv[index + 1] for index, value in enumerate(sys.argv[:-1]) if value == "--config"]
    effort = next((tomllib.loads(value)["model_reasoning_effort"] for value in config
                   if value.startswith("model_reasoning_effort=")), None)
    prompt = next(value for value in config if value.startswith("developer_instructions="))
    is_dev = task.get("public_metadata", {}).get("split") == "dev"
    emit({"type": "thread.started", "thread_id": "fixture-thread"})
    skill_result = {}
    if not mode.startswith("skills") and any(value.startswith("mcp_servers.aer_skills=") for value in config):
        import asyncio
        skill_result = asyncio.run(exercise_skills(config, call=mode.startswith("skill-evolution"),
            name="task-output-contract")) or {}
    if mode.startswith("skills"):
        import asyncio
        if mode != "skills-ignored":
            asyncio.run(exercise_skills(config, call=mode != "skills-unused"))
        if mode == "skills-patch":
            workspace = Path(sys.argv[sys.argv.index("--cd") + 1])
            (workspace / "module.py").write_text("answer = 2\n", encoding="utf-8")
        emit({"type": "item.completed", "item": {"id": "answer", "type": "agent_message", "text": '{"ok":true}'}})
        emit({"type": "turn.completed", "usage": {"input_tokens": 20, "output_tokens": 5}})
        return 0
    available = []
    if mode.startswith("tools") and mode != "tools-ignored":
        import asyncio
        try:
            available = asyncio.run(exercise_tools(config, Path(sys.argv[sys.argv.index("--cd") + 1]),
                call=mode != "tools-unused"))
        except Exception:
            emit({"type": "turn.failed", "error": {"message": "fixture tool setup failed"}})
            return 1
        if mode == "tools-validation-error" and available and not is_dev:
            emit({"type": "turn.failed", "error": {"message": "fixture validation interruption"}})
            return 1
    if mode == "quota":
        emit({"type": "error", "message": "You have hit your usage limit."})
        emit({"type": "turn.failed", "error": {"message": "You have hit your usage limit."}})
        return 1
    if mode == "recoverable-error":
        emit({"type": "error", "message": "fixture recoverable connection error"})
    if mode == "failure" or (mode == "validation-error" and not is_dev and model != "gpt-5.6-sol"):
        emit({"type": "turn.failed", "error": {"message": "fixture model unavailable"}})
        return 1
    if mode == "patch":
        workspace = Path(sys.argv[sys.argv.index("--cd") + 1])
        group = task.get("public_metadata", {}).get("validation_group")
        if model != "gpt-5.6-sol" or group == "regression":
            (workspace / "module.py").write_text("answer = 2\n", encoding="utf-8")
        emit({"type": "item.completed", "item": {"id": "answer", "type": "agent_message", "text": "Updated the source file."}})
        emit({"type": "turn.completed", "usage": {"input_tokens": 20, "output_tokens": 5}})
        return 0
    data = task["input"]["data"]
    if "expression" in data:
        answer = {"answer": calculate(ast.parse(data["expression"], mode="eval"))}
    elif "words" in data:
        answer = {"answer": ",".join(word.upper() for word in sorted(data["words"]))}
    else:
        answer = {"name": re.search(r"Name:\s*([A-Za-z]+)", data["text"]).group(1),
                  "count": int(re.search(r"Count:\s*(\d+)", data["text"]).group(1))}
    is_similar = task.get("public_metadata", {}).get("validation_group") == "similar"
    weak_selection = ("AER_SKILL_IMPROVED" not in skill_result.get("content", "") if mode.startswith("skill-evolution") else
                      not available if mode.startswith("tools") else
                      effort != "high" if mode.startswith("reasoning") else model == "gpt-5.6-sol")
    if mode in {"reject", "tools-reject", "skill-evolution-reject"} or (weak_selection and (is_dev or is_similar)) or (is_dev and mode in {"local-failure", "reasoning-local-failure"} and "IMPROVED" not in prompt):
        answer = {"answer": -999}
    emit({"type": "item.started", "item": {"id": "command-1", "type": "command_execution", "command": "fixture calculation", "status": "in_progress"}})
    emit({"type": "item.completed", "item": {"id": "command-1", "type": "command_execution", "command": "fixture calculation", "aggregated_output": "checked public input", "exit_code": 1 if mode == "tool-error" else 0, "status": "completed"}})
    text = "```json\n{}\n```" if mode == "bad-json" else json.dumps(answer)
    emit({"type": "item.completed", "item": {"id": "answer", "type": "agent_message", "text": text}})
    emit({"type": "turn.completed", **({} if mode == "missing-usage" else {"usage": {"input_tokens": 20, "cached_input_tokens": 10, "output_tokens": 5}})})
    if mode == "completed-hang":
        time.sleep(60)
    return 1 if mode == "completed-nonzero" else 0


if __name__ == "__main__":
    raise SystemExit(main())
