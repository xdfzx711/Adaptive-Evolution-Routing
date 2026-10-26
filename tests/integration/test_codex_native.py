"""Opt-in real Codex CLI test with a local Responses server; no model service."""

import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from aer_core.config.models import AgentSettings
from aer_core.schemas.references import AgentSnapshot
from aer_core.schemas.task import TaskView
from plugins.agents.codex import CodexCliAgent, CodexConfig, controlled_config
from plugins.agents.model_selection import ModelSelection
from plugins.tool_selection import ToolSelection
from plugins.agents.tool_selection import CodexToolAdapter
from plugins.workspace.local import EmptyWorkspace
from plugins.mocks import MockArtifactStore
from tests.execution.helpers import context


@unittest.skipUnless(os.name == "nt" and os.environ.get("AER_TEST_REAL_CODEX") == "1"
                     and shutil.which("codex"), "opt in with AER_TEST_REAL_CODEX=1 on Windows")
class NativeCodexTests(unittest.TestCase):
    def test_real_exec_creates_session_and_returns_structured_output_through_local_responses(self):
        models, requests = [], []
        exercise_mcp = [False]

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                body = b'{"models":[]}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                models.append(request["model"])
                requests.append(request)
                tool = next((tool for tool in request.get("tools", [])
                    if tool.get("name", "").endswith("python_syntax_check")), None)
                # Recent CLI versions also expose tools through functions.exec in
                # additional_tools input items, rather than top-level tools.
                code_mode_tool = exercise_mcp[0]
                if (tool is not None or code_mode_tool) and not any(item.get("type") in {"function_call_output", "custom_tool_call_output"}
                        for item in request.get("input", [])):
                    if tool is not None:
                        message = {"id": "fc_probe", "type": "function_call", "call_id": "call_probe",
                            "name": tool["name"], "arguments": '{"path":"sample.py"}', "status": "completed"}
                    else:
                        message = {"id": "fc_probe", "type": "custom_tool_call", "call_id": "call_probe",
                            "name": "exec", "namespace": "functions", "input":
                                'const target=ALL_TOOLS.find(t=>t.name.includes("aer_tools")&&t.name.endsWith("python_syntax_check")); '
                                'if(!target) throw Error("Expected task tool was not exposed"); '
                                'text(await tools[target.name]({path:"sample.py"}));', "status": "completed"}
                    response = {"id": "resp_tool_probe", "object": "response", "created_at": 1,
                        "model": request["model"], "status": "completed", "output": [message],
                        "usage": {"input_tokens": 3, "output_tokens": 1, "total_tokens": 4,
                                  "input_tokens_details": {"cached_tokens": 0}}}
                    field = "arguments" if tool is not None else "input"
                    prefix = "response.function_call_arguments" if tool is not None else "response.custom_tool_call_input"
                    events = [{"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
                        {"type": "response.output_item.added", "output_index": 0, "item": {**message, field: ""}},
                        {"type": prefix + ".delta", "item_id": "fc_probe", "output_index": 0, "delta": message[field]},
                        {"type": prefix + ".done", "item_id": "fc_probe", "output_index": 0, field: message[field]},
                        {"type": "response.output_item.done", "output_index": 0, "item": message},
                        {"type": "response.completed", "response": response}]
                    self.send_events(events)
                    return
                text = '{"ok":true}'
                message = {"id": "msg_probe", "type": "message", "role": "assistant",
                    "phase": "final_answer", "status": "completed",
                    "content": [{"type": "output_text", "text": text, "annotations": []}]}
                response = {"id": "resp_probe", "object": "response", "created_at": 1,
                    "model": request["model"], "status": "completed", "output": [message],
                    "usage": {"input_tokens": 3, "output_tokens": 1, "total_tokens": 4,
                              "input_tokens_details": {"cached_tokens": 0}}}
                events = [
                    {"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
                    {"type": "response.output_item.added", "output_index": 0,
                     "item": {**message, "status": "in_progress", "content": []}},
                    {"type": "response.content_part.added", "item_id": "msg_probe", "output_index": 0,
                     "content_index": 0, "part": {"type": "output_text", "text": "", "annotations": []}},
                    {"type": "response.output_text.delta", "item_id": "msg_probe", "output_index": 0,
                     "content_index": 0, "delta": text},
                    {"type": "response.output_text.done", "item_id": "msg_probe", "output_index": 0,
                     "content_index": 0, "text": text},
                    {"type": "response.output_item.done", "output_index": 0, "item": message},
                    {"type": "response.completed", "response": response},
                ]
                self.send_events(events)

            def send_events(self, events):
                body = "".join("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n"
                               for event in events).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory(prefix="aer-native-session-") as directory:
                root = Path(directory)
                config = CodexConfig(workspace_root=str(root / "workspaces"), timeout_seconds=30.0,
                                     startup_retries=0, allowed_reasoning_efforts={"gpt-5.6-sol": [None, "high"]})
                store = MockArtifactStore()
                snapshot = AgentSnapshot(version_id="native-probe", agent_plugin_id="codex_cli_agent",
                    config=controlled_config(config), artifacts={"prompt": store.put(b"Return the publicly requested JSON object", "prompt"),
                        "model": store.put(ModelSelection(model_id="gpt-5.6-sol").model_dump_json().encode(), "model")})
                agent = CodexCliAgent(AgentSettings(plugin="codex_cli_agent", initial_prompt="public probe",
                    initial_snapshot_ref=None, config=config.model_dump(mode="json")), snapshot, store)
                original = agent._arguments

                def arguments(*args):
                    argv = original(*args)
                    provider = '{name="AER local test",base_url="http://127.0.0.1:' + str(server.server_port)
                    provider += '/v1",wire_api="responses",requires_openai_auth=false,request_max_retries=0,stream_max_retries=0}'
                    return [*argv[:-1], "--config", 'model_provider="aer_test"', "--config",
                            "model_providers.aer_test=" + provider, argv[-1]]

                agent._arguments = arguments
                task = TaskView(task_id="public-probe", benchmark_id="native-probe", input={
                    "instruction": "Return an object with ok=true.",
                    "output_schema": {"type": "object", "properties": {"ok": {"type": "boolean"}},
                                      "required": ["ok"], "additionalProperties": False}})
                logical = agent.project_root / "runs" / root.name / "workspaces" / "episode" / "initial_execution"
                for index, (run_path, effort) in enumerate(((root / "run", None), (logical, None),
                                                           (root / "explicit-effort", "high")), start=1):
                    with self.subTest(run_path=str(run_path)):
                        self.assertFalse(run_path.exists())
                        selected = snapshot if effort is None else snapshot.replace(version_id="native-high",
                            parent_version_id=snapshot.version_id, artifacts={**snapshot.artifacts, "model": store.put(
                                ModelSelection(model_id="gpt-5.6-sol", reasoning_effort=effort).model_dump_json().encode(), "model")})
                        result = agent.run(task, selected, context(workspace_ref=str(run_path)))
                        self.assertEqual(result.status, "completed", result.error)
                        self.assertEqual(result.output, {"ok": True})
                        self.assertEqual(models, ["gpt-5.6-sol"] * index)
                        self.assertEqual(result.state_summary["startup_attempts"], 1)
                        self.assertTrue(result.usage.usage_complete)
                        self.assertEqual(result.usage.total_tokens, 4)
                        self.assertFalse(run_path.exists())
                        if effort is not None:
                            self.assertEqual(requests[-1]["reasoning"]["effort"], effort)
                            self.assertEqual(result.state_summary["execution_reasoning_effort"], effort)
                # A real CLI must expose the MCP tool to its model and execute a model-requested call.
                config = CodexConfig(workspace_root=str(root / "workspaces"), timeout_seconds=30.0, startup_retries=0,
                    tooling={"catalog": {"python_syntax_check": {"kind": "local", "plugin": "python_syntax_check"}}},
                    initial_tools=["python_syntax_check"])
                tool_snapshot = snapshot.replace(version_id="native-tool", config=controlled_config(config), artifacts={
                    **snapshot.artifacts, "tool": store.put(CodexToolAdapter().encode(ToolSelection(enabled_tools=["python_syntax_check"])), "tool")})
                class SourceWorkspace(EmptyWorkspace):
                    def prepare(self, task, directory, run_ctx):
                        handle = super().prepare(task, directory, run_ctx)
                        (directory / "sample.py").write_text("def example():\n return 1\n", encoding="utf-8")
                        return handle
                tool_agent = CodexCliAgent(AgentSettings(plugin="codex_cli_agent", initial_prompt="public probe",
                    initial_snapshot_ref=None, config=config.model_dump(mode="json")), tool_snapshot, store,
                    workspace_provider=SourceWorkspace())
                tool_arguments = tool_agent._arguments
                def with_tools(*args):
                    argv = tool_arguments(*args)
                    provider = '{name="AER local test",base_url="http://127.0.0.1:' + str(server.server_port)
                    provider += '/v1",wire_api="responses",requires_openai_auth=false,request_max_retries=0,stream_max_retries=0}'
                    return [*argv[:-1], "--config", 'model_provider="aer_test"', "--config", "model_providers.aer_test=" + provider, argv[-1]]
                tool_agent._arguments = with_tools
                exercise_mcp[0] = True
                result = tool_agent.run(task, tool_snapshot, context(workspace_ref=str(root / "tool-run")))
                self.assertEqual(result.status, "completed", result.error)
                self.assertEqual(result.output, {"ok": True})
                self.assertEqual(result.state_summary["tool_setup_status"], "ready")
                self.assertEqual(result.state_summary["available_tools"], ("python_syntax_check",))
                self.assertTrue(result.state_summary["tool_invocations"], result.trajectory)
                self.assertEqual(result.state_summary["tool_invocations"][0]["status"], "completed")
                self.assertEqual(result.usage.tool_calls, 1)
                self.assertTrue(any(item.get("type") in {"function_call_output", "custom_tool_call_output"}
                    for request in requests for item in request.get("input", [])))
        finally:
            server.shutdown()
            server.server_close()
