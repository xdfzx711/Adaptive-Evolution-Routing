"""Native proxy + native queue/workflow/sync, with local HTTP model fixtures."""

from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import yaml

from experiments.baselines.skillclaw import run
from experiments.baselines.skillclaw_sessions import inventory, NativeCodexLaunch
from tests.execution.helpers import context

ROOT = Path(__file__).resolve().parents[2]
OPTIONAL = all(importlib.util.find_spec(name) for name in ("fastapi", "uvicorn", "openai"))


@contextmanager
def model_server(*, native_cli=False):
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append((self.path, body))
            if self.path == "/v1/responses":
                inputs = body["input"]
                if not any(item.get("type") in {"function_call_output", "custom_tool_call_output"} for item in inputs):
                    filename = re.search(r"<location>(.*?)</location>", body["instructions"]).group(1)
                    items = [{"type": "function_call", "call_id": "read-1", "name": "read_file",
                              "arguments": json.dumps({"path": filename})}]
                    if native_cli:
                        cmd = "Get-Content -Raw -LiteralPath '" + filename.replace("'", "''") + "'"
                        items = [{"id": "fc_read", "type": "custom_tool_call", "call_id": "read-1", "name": "exec",
                            "namespace": "functions", "status": "completed",
                            "input": "text(await tools.exec_command(" + json.dumps({"cmd": cmd}) + "));"}]
                else:
                    task = {"task_id": "dev-001"} if native_cli else json.loads(inputs[0]["content"])
                    # One successful and one failed task must both reach evolution.
                    answer = {"dev-001": {"answer": 75}, "dev-002": {"answer": -1},
                              "dev-003": {"name": "Mira", "count": 6}}.get(task["task_id"], {"answer": 0})
                    items = [{"type": "message", "role": "assistant", "content": [
                        {"type": "output_text", "text": json.dumps(answer)}]}]
                items[0].update(id=items[0].get("id", "msg_fixture"), status="completed")
                if items[0]["type"] == "message":
                    items[0]["phase"] = "final_answer"
                output = {"id": "resp-fixture", "object": "response", "created_at": 1, "status": "completed",
                    "model": body["model"], "output": items,
                    "usage": {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30,
                              "input_tokens_details": {"cached_tokens": 0}}}
            else:
                system = body["messages"][0]["content"]
                if "session-level evaluator" in system:
                    text = json.dumps({"task_completion": 0.5, "response_quality": 0.5,
                        "efficiency": 1, "tool_usage": 1, "overall_score": 0.6, "rationale": "Fixture evidence"})
                elif "skill evolution system" in system:
                    text = json.dumps({"action": "improve_skill", "rationale": "Clarify the output rule",
                        "skill": {"name": "fixture-skill", "description": "Read for fixture tasks.",
                                  "content": "Use the public task output contract. IMPROVED"}})
                elif "skill engineer" in system:
                    text = json.dumps({"action": "skip", "rationale": "No evidence of a missing skill"})
                else:
                    text = "The session has observable task output and a skill read."
                output = {"id": "chat-fixture", "object": "chat.completion", "created": 1, "model": body["model"],
                          "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}]}
            if body.get("stream") and self.path.endswith("responses"):
                item = output["output"][0]
                events = [{"type": "response.created", "response": {**output, "status": "in_progress", "output": []}},
                    {"type": "response.output_item.added", "output_index": 0, "item": item},
                    {"type": "response.output_item.done", "output_index": 0, "item": item},
                    {"type": "response.completed", "response": output}]
                encoded = "".join("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n" for event in events).encode()
                content_type = "text/event-stream"
            else:
                encoded = json.dumps(output).encode()
                content_type = "application/json"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def config(root, *, mode="learn", extra_args=(), task_ids=None):
    data = yaml.safe_load((ROOT / "experiments/configs/v0_1_codex_skillclaw_baseline_tasks.yaml").read_text())
    data["benchmark"].update(plugin="toy_tasks", task_ids=task_ids or ["dev-001", "dev-002", "dev-003"])
    data["storage"]["root"] = str(root / "runs")
    data["agent"]["config"].update(command=[sys.executable,
        str(ROOT / "tests/execution/fake_codex_skillclaw.py"), *extra_args], workspace_root=str(root / "workspaces"))
    task_config = root / "tasks.yaml"
    task_config.write_text(yaml.safe_dump(data), encoding="utf-8")
    library = root / "initial" / "fixture-skill"
    library.mkdir(parents=True)
    (library / "SKILL.md").write_text("---\nname: fixture-skill\ndescription: Read for fixture tasks.\n---\nUse the public task output contract.\n", encoding="utf-8")
    (library / "assets").mkdir()
    (library / "assets/data.bin").write_bytes(b"\x00\xff\x01")
    wrapper = {"aer_config": str(task_config), "initial_library": str(library.parent),
        "source": str(ROOT / "third_party/SkillClaw"), "mode": mode, "dotenv_path": None,
        "evolve_every_tasks": 2, "cycle_timeout_seconds": 30,
        "execution_api": {"api_key_env": "FIXTURE_TASK_KEY", "base_url_env": "FIXTURE_BASE"},
        "evolution_api": {"api_key_env": "FIXTURE_EVOLVE_KEY", "base_url_env": "FIXTURE_BASE", "model_env": "FIXTURE_MODEL"}}
    path = root / "baseline.yaml"
    path.write_text(yaml.safe_dump(wrapper), encoding="utf-8")
    return path


@unittest.skipUnless(OPTIONAL, "Install the skillclaw-baseline extra")
class SkillClawBaselineTests(unittest.TestCase):
    def environment(self, url):
        return patch.dict(os.environ, {"FIXTURE_TASK_KEY": "fixture-task", "FIXTURE_EVOLVE_KEY": "fixture-evolve",
                                      "FIXTURE_BASE": url, "FIXTURE_MODEL": "qwen-fixture"})

    def test_cross_task_native_learning_publication_and_next_task_reads_new_bundle(self):
        with tempfile.TemporaryDirectory() as directory, model_server() as (url, calls), self.environment(url):
            root = Path(directory)
            path = config(root)
            initial = inventory(root / "initial")
            with patch("experiments.execution.PreparedExecution.run_task", side_effect=AssertionError("No AER diagnosis/cache")):
                result = run(path)
            report = json.loads(Path(result["report"]).read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "completed")
            self.assertEqual([item["score"] for item in report["tasks"]], [1, 0, 1])
            self.assertEqual(report["cycles"][0]["summary"]["sessions"], 2)
            self.assertEqual(report["cycles"][0]["summary"]["skill_groups"], 1)
            self.assertEqual(report["cycles"][0]["summary"]["no_skill_sessions"], 0)
            self.assertGreater(report["cycles"][0]["summary"]["uploaded_skills"], 0)
            self.assertEqual(report["tasks"][0]["library_digest"], report["tasks"][1]["library_digest"])
            self.assertNotEqual(report["tasks"][1]["library_digest"], report["tasks"][2]["library_digest"])
            task = json.loads(Path(report["tasks"][2]["record"]).read_text(encoding="utf-8"))
            runtime = task["execution"]["state_summary"]["external_runtime"]
            self.assertTrue(runtime["uploaded"])
            self.assertTrue(runtime["read_receipts"])
            expected = inventory(result["library"])["files"]["fixture-skill/SKILL.md"]
            self.assertEqual(runtime["read_receipts"][0]["sha256"], expected)
            self.assertTrue(runtime["read_receipts"][0]["matches_supplied_version"])
            # Task staging preserves complete bundles. The pinned workflow's
            # native publication replaces a changed skill with Markdown only;
            # the adapter must expose that deletion rather than silently repair it.
            initial_copy = Path(result["report"]).parent / "libraries" / initial["digest"]
            self.assertEqual((initial_copy / "fixture-skill/assets/data.bin").read_bytes(), b"\x00\xff\x01")
            self.assertIn("fixture-skill/assets/data.bin", report["cycles"][0]["removed_files"])
            self.assertEqual(inventory(root / "initial"), initial)
            self.assertFalse(list((root / "workspaces").glob("*/.aer-skillclaw-*")))
            self.assertFalse(list(Path(result["report"]).parent.glob("shared/baseline/sessions/*.json")))
            for archive in Path(result["report"]).parent.glob("sessions/*.json"):
                session = json.loads(archive.read_text(encoding="utf-8"))
                self.assertNotIn("evaluation", session)
                self.assertNotIn("orm_score", session)
            self.assertTrue(all(body["model"] == "qwen-fixture" for route, body in calls if route.endswith("completions")))
            self.assertTrue(all(body["model"] == "gpt-6.1-sol" for route, body in calls if route.endswith("responses")))
            self.assertIsNone(report["evolution_usage"])

    def test_catalog_only_stays_no_skill_and_skip_is_successful_cycle(self):
        with tempfile.TemporaryDirectory() as directory, model_server() as (url, calls), self.environment(url):
            path = config(Path(directory), extra_args=["--no-read"], task_ids=["dev-001"])
            result = run(path)
            report = json.loads(Path(result["report"]).read_text(encoding="utf-8"))
            self.assertEqual(report["cycles"][0]["summary"]["no_skill_sessions"], 1)
            self.assertEqual(report["cycles"][0]["summary"]["uploaded_skills"], 0)
            self.assertEqual(report["initial_library"], report["final_library"])

    def test_frozen_never_uploads_or_calls_evolution_model(self):
        with tempfile.TemporaryDirectory() as directory, model_server() as (url, calls), self.environment(url):
            path = config(Path(directory), mode="frozen", task_ids=["dev-001"])
            result = run(path)
            report = json.loads(Path(result["report"]).read_text(encoding="utf-8"))
            self.assertEqual(report["cycles"], [])
            self.assertTrue(all(route.endswith("responses") for route, _ in calls))
            self.assertFalse((Path(result["report"]).parent / "shared").exists())

    def test_frozen_snapshot_runs_test_split_without_qwen_credentials(self):
        with tempfile.TemporaryDirectory() as directory, model_server() as (url, calls), self.environment(url):
            root = Path(directory)
            path = config(root, mode="frozen", task_ids=["dev-001"])
            first = run(path)
            source = json.loads(Path(first["report"]).read_text(encoding="utf-8"))
            tasks = root / "tasks.yaml"
            data = yaml.safe_load(tasks.read_text())
            data["benchmark"].update(execution_split="test", task_ids=["test-001"])
            data["agent"].update(initial_prompt=None, initial_snapshot_ref=source["aer_snapshot_ref"])
            tasks.write_text(yaml.safe_dump(data), encoding="utf-8")
            with patch.dict(os.environ):
                os.environ.pop("FIXTURE_EVOLVE_KEY")
                os.environ.pop("FIXTURE_MODEL")
                result = run(path)
            report = json.loads(Path(result["report"]).read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "completed")
            self.assertEqual(report["tasks"][0]["task_id"], "test-001")
            self.assertEqual(report["cycles"], [])
            self.assertEqual(report["initial_library"], report["final_library"])
            self.assertTrue(all(route.endswith("responses") for route, _ in calls))

    def test_modified_library_aborts_and_cleans_before_collecting(self):
        with tempfile.TemporaryDirectory() as directory, model_server() as (url, _), self.environment(url):
            root = Path(directory)
            path = config(root, extra_args=["--modify-skill"], task_ids=["dev-001"])
            with self.assertRaisesRegex(RuntimeError, "session collection"):
                run(path)
            report = json.loads(next(root.glob("runs/*/skillclaw-baseline/baseline.json")).read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "error")
            self.assertEqual(report["cycles"], [])
            self.assertFalse(list((root / "workspaces").glob("*/.aer-skillclaw-*")))

    @unittest.skipUnless(os.name == "nt" and os.environ.get("AER_TEST_REAL_CODEX") == "1"
                         and shutil.which("codex"), "Opt in to real Codex with local HTTP models")
    def test_real_codex_reads_staged_skill_through_native_streaming_proxy(self):
        with tempfile.TemporaryDirectory() as directory, model_server(native_cli=True) as (url, calls), self.environment(url):
            root = Path(directory)
            path = config(root, mode="frozen", task_ids=["dev-001"])
            tasks = root / "tasks.yaml"
            data = yaml.safe_load(tasks.read_text())
            data["agent"]["config"].update(command=["codex"], timeout_seconds=45, startup_retries=0)
            tasks.write_text(yaml.safe_dump(data), encoding="utf-8")
            try:
                result = run(path)
            except Exception:
                for record in root.glob("runs/*/skillclaw-baseline/task-*.json"):
                    print(record.read_text(encoding="utf-8"))
                raise
            report = json.loads(Path(result["report"]).read_text(encoding="utf-8"))
            record = json.loads(Path(report["tasks"][0]["record"]).read_text(encoding="utf-8"))
            self.assertEqual(report["tasks"][0]["score"], 1)
            outputs = [item for _, request in calls for item in request.get("input", [])
                       if item.get("type") in {"function_call_output", "custom_tool_call_output"}]
            self.assertTrue(record["execution"]["state_summary"]["external_runtime"]["read_receipts"],
                            json.dumps(outputs, ensure_ascii=False))


class SkillClawBaselineBoundaryTests(unittest.TestCase):
    def test_prepare_rejects_test_learning_and_existing_aer_routing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = config(root)
            tasks = root / "tasks.yaml"
            data = yaml.safe_load(tasks.read_text())
            data["benchmark"]["execution_split"] = "test"
            tasks.write_text(yaml.safe_dump(data), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "frozen Snapshot|dev tasks"):
                run(path, prepare_only=True)

    def test_staging_does_not_enter_git_patch(self):
        from plugins.workspace.git import git
        from plugins.workspace.collectors import GitPatchCollector, PatchCollectorSettings
        from plugins.mocks import MockArtifactStore
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config(root)
            workspace = root / "workspace"
            workspace.mkdir()
            git(workspace, "init")
            (workspace / "app.py").write_text("x = 1\n")
            git(workspace, "add", ".")
            git(workspace, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "base")
            revision = git(workspace, "rev-parse", "HEAD").strip()
            client = SimpleNamespace(request=lambda *_: {}, url="http://127.0.0.1:1", proxy_key="fixture")
            session = NativeCodexLaunch(client, root / "initial").prepare(workspace, context())
            (workspace / "app.py").write_text("x = 2\n")
            session.finish()
            result = GitPatchCollector(PatchCollectorSettings(), MockArtifactStore()).collect(None,
                SimpleNamespace(path=workspace, baseline_revision=revision, source_revision=revision), "", context())
            self.assertEqual(list(result.output["changed_files"]), ["app.py"])
            self.assertNotIn("SKILL.md", result.output["patch"])
