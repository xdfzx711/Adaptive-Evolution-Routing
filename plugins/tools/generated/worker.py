"""Jupyter worker protocol. No controller imports or provider credentials are needed."""

import argparse
import ast
import json
from pathlib import Path
import queue
import sys
from time import monotonic


def execute(data, workspace):
    from jupyter_client import KernelManager

    manager = KernelManager(kernel_name="python3")
    # Use this worker's interpreter even if a user's python3 kernelspec points elsewhere.
    manager.kernel_spec.argv = [sys.executable, "-m", "ipykernel_launcher", "-f", "{connection_file}"]
    client = None
    deadline = monotonic() + data["timeout_seconds"]
    try:
        manager.start_kernel(cwd=str(workspace))
        client = manager.client()
        client.start_channels()
        client.wait_for_ready(timeout=max(0.1, deadline - monotonic()))
        payload = json.dumps({"tool": data["tool"], "arguments": data["arguments"], "workspace": str(workspace)})
        code = (
            "import json as __aer_json, inspect as __aer_inspect, sys as __aer_sys, types as __aer_types\n"
            f"__aer_data = __aer_json.loads({payload!r})\n"
            "__aer_sys.path.insert(0, __aer_data['workspace'])\n"
            "__aer_module = __aer_types.ModuleType('aer_generated_tool')\n"
            "__aer_sys.modules['aer_generated_tool'] = __aer_module\n"
            "__aer_namespace = __aer_module.__dict__\n"
            "exec(compile(__aer_data['tool']['source'], '<generated-tool>', 'exec'), __aer_namespace)\n"
            "__aer_callable = __aer_namespace[__aer_data['tool']['entrypoint']]\n"
            "if __aer_data['tool']['kind'] == 'class':\n"
            "    __aer_callable = __aer_callable(**__aer_data['tool']['constructor_arguments'])\n"
            "__aer_arguments = dict(__aer_data['arguments'])\n"
            "if 'workspace' in __aer_inspect.signature(__aer_callable).parameters:\n"
            "    __aer_arguments['workspace'] = __aer_data['workspace']\n"
            "__aer_inspect.signature(__aer_callable).bind(**__aer_arguments)\n"
            "__aer_result = __aer_callable(**__aer_arguments)\n"
            "__aer_serialized = __aer_json.dumps(__aer_result, ensure_ascii=False, allow_nan=False)\n"
        )
        message_id = client.execute(code, user_expressions={"result": "__aer_serialized"}, stop_on_error=True)
        stdout, errors = [], []
        size = 0
        while True:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError("Tool kernel timed out")
            try:
                message = client.get_iopub_msg(timeout=min(remaining, 0.5))
            except queue.Empty:
                continue
            if message.get("parent_header", {}).get("msg_id") != message_id:
                continue
            content, kind = message["content"], message["msg_type"]
            if kind == "stream":
                size += len(content["text"])
                if size > 1024 * 1024:
                    raise ValueError("Tool output exceeds 1 MiB")
                stdout.append(content["text"])
            elif kind == "error":
                errors.append(content["ename"] + ": " + content["evalue"])
            elif kind == "status" and content["execution_state"] == "idle":
                break
        while True:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError("Tool reply timed out")
            reply = client.get_shell_msg(timeout=remaining)
            if reply.get("parent_header", {}).get("msg_id") == message_id:
                break
        content = reply["content"]
        if content["status"] != "ok" or errors:
            return {"status": "error", "result": None, "stdout": "".join(stdout),
                    "error": "\n".join(errors) or content.get("evalue", "Tool execution failed")}
        expression = content["user_expressions"]["result"]
        if expression["status"] != "ok":
            raise ValueError("Tool must return a JSON-serializable result")
        serialized = ast.literal_eval(expression["data"]["text/plain"])
        if len(serialized) > 1024 * 1024:
            raise ValueError("Tool result exceeds 1 MiB")
        return {"status": "completed", "result": json.loads(serialized), "stdout": "".join(stdout), "error": None}
    finally:
        if client is not None:
            client.stop_channels()
        if manager.has_kernel:
            manager.shutdown_kernel(now=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--workspace", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = execute(json.loads(args.input.read_text(encoding="utf-8")), args.workspace)
    except Exception as failure:
        result = {"status": "error", "result": None, "stdout": "", "error": type(failure).__name__ + ": " + str(failure)}
    args.output.write_text(json.dumps(result, ensure_ascii=False, allow_nan=False), encoding="utf-8")


if __name__ == "__main__":
    main()
