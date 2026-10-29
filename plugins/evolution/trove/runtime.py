"""Execute portable JSON functions with a timeout and bounded protocol output."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from time import monotonic

import jsonschema

from .library import ToolLibrary


MAX_OUTPUT_BYTES = 1024 * 1024


def stop_worker(process):
    # Windows virtualenv Python may launch a second interpreter. Killing only the
    # launcher leaves that child holding the temporary working directory open.
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW, check=False)
    if process.poll() is None:
        process.kill()
    process.wait(timeout=5)


def run_python(library: ToolLibrary, *, timeout: float, name=None, arguments=None, solution=None, task=None):
    payload = {"library": library.model_dump(mode="json"),
        **({"operation": "call", "name": name, "arguments": arguments} if name is not None else
           {"operation": "solve", "solution": solution, "task": task})}
    worker = Path(__file__).with_name("worker.py")
    started = monotonic()
    # Keep task repositories, environment credentials and user site packages out of the worker.
    env = {key: value for key, value in os.environ.items() if key.upper() in {
        "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LANG", "LC_ALL"}}
    try:
        serialized = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        with tempfile.TemporaryDirectory(prefix="aer-trove-call-") as directory:
            with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
                process = subprocess.Popen([sys.executable, "-I", str(worker)], stdin=subprocess.PIPE,
                    stdout=output, stderr=errors, cwd=directory, env=env,
                    **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}))
                try:
                    process.communicate(serialized, timeout=timeout)
                except subprocess.TimeoutExpired:
                    stop_worker(process)
                    return {"ok": False, "error": "timeout", "elapsed_seconds": monotonic() - started}
                if process.returncode:
                    return {"ok": False, "error": "worker_failed", "exit_code": process.returncode,
                        "elapsed_seconds": monotonic() - started}
                output.seek(0)
                data = output.read(MAX_OUTPUT_BYTES + 1)
                if len(data) > MAX_OUTPUT_BYTES:
                    return {"ok": False, "error": "output_limit", "elapsed_seconds": monotonic() - started}
                result = json.loads(data)
    except (OSError, ValueError, TypeError) as failure:
        result = {"ok": False, "error": "worker_failed", "error_type": type(failure).__name__}
    return {**result, "elapsed_seconds": monotonic() - started}


def call_tool(library, name, arguments, timeout):
    tool = next(tool for tool in library.tools if tool.name == name)
    jsonschema.validate(arguments, tool.model_dump(mode="json")["input_schema"])
    return run_python(library, timeout=timeout, name=name, arguments=arguments)
