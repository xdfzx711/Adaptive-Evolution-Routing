"""Fresh Python process for JSON-only toolbox calls; never import generated modules."""

import __future__
import builtins
import contextlib
import importlib
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from plugins.evolution.trove.library import MODULE_EXPORTS, ToolLibrary, validate_source


def safe_import(name, globals=None, locals=None, fromlist=(), level=0):
    if level or name not in MODULE_EXPORTS:
        raise ImportError("Unsupported import")
    module = importlib.import_module(name)
    return SimpleNamespace(**{key: getattr(module, key) for key in MODULE_EXPORTS[name].split()})


def execute(payload):
    library = ToolLibrary.model_validate(payload["library"])
    names = ("abs all any bool dict enumerate filter float frozenset int isinstance issubclass len list map max min "
             "next pow range repr reversed round set slice sorted str sum tuple zip "
             "Exception ValueError TypeError KeyError IndexError StopIteration ZeroDivisionError AssertionError")
    env = {"__builtins__": {name: getattr(builtins, name) for name in names.split()}}
    env["__builtins__"]["__import__"] = safe_import
    used = []

    def counted(function, name):
        def call(*args, **kwargs):
            used.append(name)
            return function(*args, **kwargs)
        return call

    for tool in library.ordered_tools():
        exec(compile(tool.source, "<trove-tool>", "exec", flags=__future__.annotations.compiler_flag), env)
        env[tool.name] = counted(env[tool.name], tool.name)
    if payload["operation"] == "call":
        result = env[payload["name"]](**payload["arguments"])
    else:
        validate_source(payload["solution"], "solve")
        env["task"] = payload["task"]
        exec(compile(payload["solution"], "<trove-solution>", "exec", flags=__future__.annotations.compiler_flag), env)
        result = env["solve"]()
    if not isinstance(result, dict):
        raise ValueError("Tool and solution outputs must be JSON objects")
    # Reject non-finite and non-JSON outputs in the child, before writing a receipt.
    json.dumps(result, allow_nan=False)
    return {"ok": True, "output": result, "used_tools": used}


def main():
    try:
        payload = json.load(sys.stdin)
        # Tool print output must never interfere with the JSON protocol.
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = execute(payload)
    except Exception as failure:
        result = {"ok": False, "error": type(failure).__name__}
    sys.stdout.write(json.dumps(result, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
