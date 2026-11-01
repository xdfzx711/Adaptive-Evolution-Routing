"""One bounded worker per call, with an actual task cwd and a fresh Python kernel."""

import json
from functools import lru_cache
import os
from pathlib import Path
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import monotonic

import jsonschema
from .paths import bind_repository_paths


@lru_cache(maxsize=8)
def runtime_identity(python_executable=None):
    """Read interpreter/package versions without importing tools or starting a kernel."""
    script = (
        "import json, sys, importlib.metadata as m; "
        "print(json.dumps({'python':sys.version,'executable':sys.executable,"
        "'packages':sorted((d.metadata.get('Name',''),d.version) for d in m.distributions())}))"
    )
    try:
        result = subprocess.run([python_executable or sys.executable, "-c", script], capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=15, check=True,
            **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}))
        return json.loads(result.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return {"python_executable": python_executable or sys.executable, "status": "unavailable"}


def stop_tree(process):
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True,
                       timeout=10, creationflags=subprocess.CREATE_NO_WINDOW, check=False)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.kill()
    process.wait(timeout=10)


def execute_generated(tool, arguments, workspace, *, timeout_seconds=30.0, python_executable=None):
    """This is process/workspace isolation, not an operating-system filesystem sandbox."""
    data = tool.model_dump(mode="json")
    workspace = Path(workspace).resolve(strict=True)
    arguments, _ = bind_repository_paths(arguments, data["input_schema"], workspace)
    jsonschema.validate(arguments, data["input_schema"])
    started = monotonic()
    with TemporaryDirectory(prefix="aer-generated-call-") as directory:
        root = Path(directory)
        request, output = root / "request.json", root / "result.json"
        request.write_text(json.dumps({"tool": data, "arguments": arguments,
            "timeout_seconds": timeout_seconds}, ensure_ascii=False), encoding="utf-8")
        # Generated code does not need the controller's model/provider credentials.
        allowed = {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "TMPDIR",
                   "USERPROFILE", "HOME", "APPDATA", "LOCALAPPDATA", "LANG", "LC_ALL"}
        environment = {key: value for key, value in os.environ.items() if key.upper() in allowed}
        environment.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1", JUPYTER_RUNTIME_DIR=str(root))
        worker = Path(__file__).with_name("worker.py")
        with open(os.devnull, "wb") as null:
            process = subprocess.Popen([python_executable or sys.executable, str(worker), "--input", str(request),
                "--output", str(output), "--workspace", str(workspace)], cwd=workspace, env=environment,
                stdin=subprocess.DEVNULL, stdout=null, stderr=null,
                **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}))
            try:
                process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                stop_tree(process)
                return {"status": "error", "result": None, "stdout": "", "error": "Tool execution timed out",
                        "elapsed_seconds": monotonic() - started}
            finally:
                if process.poll() is None:
                    stop_tree(process)
        if not output.is_file():
            raise RuntimeError("Generated-tool worker failed before returning a result; check the runtime environment")
        result = json.loads(output.read_text(encoding="utf-8"))
        result["elapsed_seconds"] = monotonic() - started
        return result
