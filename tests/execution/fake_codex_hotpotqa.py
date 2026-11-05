"""Offline protocol fixture; answers synthetic questions using public input and real MCP."""

import asyncio
import json
import sys
import tomllib


def emit(value):
    print(json.dumps(value), flush=True)


def public_answer(context):
    for paragraph in context:
        for sentence in paragraph["sentences"]:
            if sentence["text"].startswith("Answer: "):
                return {"answer": sentence["text"][8:],
                        "supporting_facts": [[paragraph["title"], sentence["sentence_id"]]]}
    return {"answer": "unknown", "supporting_facts": []}


async def invoke(config, context):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    server = next((tomllib.loads(value)["mcp_servers"]["aer_tools"] for value in config
                   if value.startswith("mcp_servers.aer_tools=")), None)
    if server is None:
        return None
    async with stdio_client(StdioServerParameters(command=server["command"], args=server["args"])) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            assert [tool.name for tool in tools] == ["locate_evidence"], "Unexpected cross-episode tool state"
            arguments = {"context": context}
            result = await session.call_tool("locate_evidence", arguments)
            emit({"type": "item.completed", "item": {"id": "mcp-1", "type": "mcp_tool_call",
                "server": "aer_tools", "tool": "locate_evidence", "arguments": arguments,
                "result": result.model_dump(mode="json"), "status": "failed" if result.isError else "completed"}})
            if result.isError:
                raise RuntimeError("Fixture generated tool failed")
            return result.structuredContent["result"]


def main():
    if "--version" in sys.argv:
        print("fake-codex-hotpotqa 1")
        return
    mode = sys.argv[sys.argv.index("--fixture-mode") + 1]
    config = [sys.argv[i + 1] for i, arg in enumerate(sys.argv[:-1]) if arg == "--config"]
    task = json.load(sys.stdin)
    emit({"type": "thread.started", "thread_id": "hotpotqa-fixture"})
    emit({"type": "turn.started"})
    context = task["input"]["context"]
    answer = asyncio.run(invoke(config, context))
    regression = task["public_metadata"].get("validation_group") == "regression"
    if answer is None:
        answer = public_answer(context) if regression or mode == "perfect" else {"answer": "wrong", "supporting_facts": []}
    elif mode == "reject" and regression:
        # A controlled task-level regression, independent of the tool's execution success.
        answer = {"answer": "wrong", "supporting_facts": []}
    emit({"type": "item.completed", "item": {"id": "answer", "type": "agent_message", "text": json.dumps(answer)}})
    emit({"type": "turn.completed", "usage": {"input_tokens": 20, "output_tokens": 10}})


if __name__ == "__main__":
    main()
