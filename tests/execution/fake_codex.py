"""Offline CLI fixture: reads public tasks only and emits Codex-shaped events."""

import ast
import json
import operator
from pathlib import Path
import re
import sys
import time


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
    prompt = next(value for value in config if value.startswith("developer_instructions="))
    is_dev = task.get("public_metadata", {}).get("split") == "dev"
    emit({"type": "thread.started", "thread_id": "fixture-thread"})
    if mode == "recoverable-error":
        emit({"type": "error", "message": "fixture recoverable connection error"})
    if mode == "failure" or (mode == "validation-error" and not is_dev and model != "gpt-5.6-sol"):
        emit({"type": "turn.failed", "error": {"message": "fixture model unavailable"}})
        return 1
    data = task["input"]["data"]
    if "expression" in data:
        answer = {"answer": calculate(ast.parse(data["expression"], mode="eval"))}
    elif "words" in data:
        answer = {"answer": ",".join(word.upper() for word in sorted(data["words"]))}
    else:
        answer = {"name": re.search(r"Name:\s*([A-Za-z]+)", data["text"]).group(1),
                  "count": int(re.search(r"Count:\s*(\d+)", data["text"]).group(1))}
    is_similar = task.get("public_metadata", {}).get("validation_group") == "similar"
    if mode == "reject" or (model == "gpt-5.6-sol" and (is_dev or is_similar)) or (is_dev and mode == "local-failure" and "IMPROVED" not in prompt):
        answer = {"answer": -999}
    emit({"type": "item.started", "item": {"id": "command-1", "type": "command_execution", "command": "fixture calculation", "status": "in_progress"}})
    emit({"type": "item.completed", "item": {"id": "command-1", "type": "command_execution", "command": "fixture calculation", "aggregated_output": "checked public input", "exit_code": 1 if mode == "tool-error" else 0, "status": "completed"}})
    text = "```json\n{}\n```" if mode == "bad-json" else json.dumps(answer)
    emit({"type": "item.completed", "item": {"id": "answer", "type": "agent_message", "text": text}})
    emit({"type": "turn.completed", **({} if mode == "missing-usage" else {"usage": {"input_tokens": 20, "cached_input_tokens": 10, "output_tokens": 5}})})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
