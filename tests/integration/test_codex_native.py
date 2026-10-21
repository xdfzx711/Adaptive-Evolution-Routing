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
from plugins.agents.codex import CodexCliAgent, CodexConfig
from plugins.agents.model_selection import ModelSelection
from plugins.mocks import MockArtifactStore
from tests.execution.helpers import context


@unittest.skipUnless(os.name == "nt" and os.environ.get("AER_TEST_REAL_CODEX") == "1"
                     and shutil.which("codex"), "opt in with AER_TEST_REAL_CODEX=1 on Windows")
class NativeCodexTests(unittest.TestCase):
    def test_real_exec_creates_session_and_returns_structured_output_through_local_responses(self):
        models = []

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
                                     startup_retries=0)
                controlled = config.model_dump(mode="json")
                controlled.pop("initial_model")
                store = MockArtifactStore()
                snapshot = AgentSnapshot(version_id="native-probe", agent_plugin_id="codex_cli_agent",
                    config=controlled, artifacts={"prompt": store.put(b"Return the publicly requested JSON object", "prompt"),
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
                for index, run_path in enumerate((root / "run", logical), start=1):
                    with self.subTest(run_path=str(run_path)):
                        self.assertFalse(run_path.exists())
                        result = agent.run(task, snapshot, context(workspace_ref=str(run_path)))
                        self.assertEqual(result.status, "completed", result.error)
                        self.assertEqual(result.output, {"ok": True})
                        self.assertEqual(models, ["gpt-5.6-sol"] * index)
                        self.assertEqual(result.state_summary["startup_attempts"], 1)
                        self.assertTrue(result.usage.usage_complete)
                        self.assertEqual(result.usage.total_tokens, 4)
                        self.assertFalse(run_path.exists())
        finally:
            server.shutdown()
            server.server_close()
