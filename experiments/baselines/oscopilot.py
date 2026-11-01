"""Launch the unmodified pinned OS-Copilot quick_start in a separate environment."""

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess

from dotenv import dotenv_values

from plugins.evolution.oscopilot.source import DEFAULT_SOURCE, UPSTREAM_REVISION, load_upstream
from plugins.tools.generated.runtime import stop_tree


def prepare_run(*, python, query, workspace, output_root, source_path=None):
    source = Path(source_path or DEFAULT_SOURCE).resolve()
    load_upstream(str(source))  # Verify the same pristine revision used by the plugin.
    interpreter = Path(python).expanduser().resolve(strict=True)
    workspace = Path(workspace).expanduser().resolve(strict=True)
    if not workspace.is_dir():
        raise ValueError("Baseline workspace must be a directory")
    root = Path(output_root).expanduser().resolve()
    if root == source or root.is_relative_to(source) or root == workspace or root.is_relative_to(workspace):
        raise ValueError("Baseline output/tool repository must be outside upstream source and the task workspace")
    root.mkdir(parents=True, exist_ok=False)
    shutil.copytree(source / "oscopilot/tool_repository/generated_tools", root / "tool-repository")
    (root / "logs").mkdir()
    query_path = root / "query.txt"
    query_path.write_text(query, encoding="utf-8")
    command = [str(interpreter), str(source / "quick_start.py"), "--query", query,
        "--working_dir", str(workspace), "--generated_tool_repo_path", str(root / "tool-repository"),
        "--logging_filedir", str(root / "logs"), "--logging_filename", "native.log"]
    manifest = {"baseline": "os_copilot_native", "upstream_revision": UPSTREAM_REVISION,
        "source_path": str(source), "python": str(interpreter), "workspace": str(workspace),
        "tool_repository": str(root / "tool-repository"), "query_file": str(query_path),
        "command": command, "status": "prepared", "source_patches": [],
        "semantics": "Native FridayAgent; upstream prompts, execution environment, judgment, repair and Chroma retrieval",
        "usage": None, "task_success": None}
    (root / "baseline.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return root, manifest


def run_native(root, manifest, *, dotenv_path=None, model=None, timeout_seconds=600.0):
    environment = dict(os.environ)
    if dotenv_path:
        environment.update({key: value for key, value in dotenv_values(dotenv_path, interpolate=False).items() if value is not None})
    if model:
        environment["MODEL_NAME"] = model
    environment.setdefault("MODEL_TYPE", "OpenAI")
    environment.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    environment["PYTHONPATH"] = manifest["source_path"]
    started = datetime.now().astimezone().isoformat()
    with (root / "stdout.txt").open("wb") as stdout, (root / "stderr.txt").open("wb") as stderr:
        process = subprocess.Popen(manifest["command"], cwd=manifest["workspace"], env=environment, stdin=subprocess.DEVNULL,
            stdout=stdout, stderr=stderr,
            **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}))
        try:
            code = process.wait(timeout=timeout_seconds)
            status = "process_completed" if code == 0 else "process_error"
        except subprocess.TimeoutExpired:
            stop_tree(process)
            code, status = process.returncode, "timeout"
        finally:
            if process.poll() is None:
                stop_tree(process)
    manifest.update(status=status, exit_code=code, started_at=started,
                    finished_at=datetime.now().astimezone().isoformat())
    # Native run() may swallow task errors and return None. A zero exit code is not a task success label.
    (root / "baseline.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", required=True, type=Path, help="Interpreter from a separately installed upstream environment")
    query = parser.add_mutually_exclusive_group(required=True)
    query.add_argument("--query")
    query.add_argument("--query-file", type=Path)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--source-path", type=Path)
    parser.add_argument("--dotenv", type=Path)
    parser.add_argument("--model")
    parser.add_argument("--timeout-seconds", type=float, default=600.0)
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args(argv)
    if args.timeout_seconds <= 0:
        parser.error("timeout-seconds must be positive")
    text = args.query_file.read_text(encoding="utf-8") if args.query_file else args.query
    root, manifest = prepare_run(python=args.python, query=text, workspace=args.workspace,
                                output_root=args.output_root, source_path=args.source_path)
    if not args.prepare_only:
        manifest = run_native(root, manifest, dotenv_path=args.dotenv, model=args.model, timeout_seconds=args.timeout_seconds)
    print(json.dumps({"manifest": str(root / "baseline.json"), "status": manifest["status"]}, ensure_ascii=False))
    return 0 if manifest["status"] in {"prepared", "process_completed"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
