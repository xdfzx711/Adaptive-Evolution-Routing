"""Run native FridayExecutor programs without requiring a public tool interface."""

from types import SimpleNamespace

from plugins.tools.generated.runtime import execute_generated


def execute_program(code, workspace, *, timeout_seconds, python_executable=None):
    # Reuse the bounded, credential-filtered worker transport, not GeneratedTool validation.
    # The native executor supplies the full program, including its invocation and return markers.
    source = (
        "def aer_native_program(workspace):\n"
        "    import json, sys, types\n"
        "    module = types.ModuleType('__oscopilot__')\n"
        "    sys.modules[module.__name__] = module\n"
        "    namespace = module.__dict__\n"
        "    namespace['workspace'] = workspace\n"
        f"    exec(compile({code!r}, '<oscopilot>', 'exec'), namespace)\n"
        "    value = namespace.get('result')\n"
        "    try:\n"
        "        json.dumps(value, allow_nan=False)\n"
        "    except (TypeError, ValueError):\n"
        "        value = repr(value)\n"
        "    return value\n"
    )
    packet = {"source": source, "entrypoint": "aer_native_program", "kind": "function",
              "constructor_arguments": {}, "input_schema": {"type": "object"}}
    return execute_generated(SimpleNamespace(model_dump=lambda **_: packet), {}, workspace,
                             timeout_seconds=timeout_seconds, python_executable=python_executable)


class NativeEnvironment:
    def __init__(self, workspace, timeout_seconds, python_executable=None):
        self.workspace = workspace
        self.working_dir = str(workspace)
        self.timeout_seconds = timeout_seconds
        self.python_executable = python_executable
        self.last_result = None

    def step(self, node_type, code):
        if node_type != "Python":
            raise ValueError("Only Python construction programs are supported")
        self.last_result = execute_program(code, self.workspace, timeout_seconds=self.timeout_seconds,
                                          python_executable=self.python_executable)
        result = self.last_result
        return SimpleNamespace(error=result["error"], result=result["stdout"] or str(result["result"]),
                               pwd=self.working_dir,
                               ls="\n".join(sorted(path.name for path in self.workspace.iterdir())))
