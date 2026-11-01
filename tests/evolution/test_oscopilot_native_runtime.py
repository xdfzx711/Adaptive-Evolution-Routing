"""The original executor runs programs before any Codex publication conversion."""

from contextlib import redirect_stdout
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest

from plugins.evolution.oscopilot.native_runtime import NativeEnvironment
from plugins.evolution.oscopilot.source import DEFAULT_SOURCE, load_upstream


@unittest.skipUnless(importlib.util.find_spec("ipykernel") and (DEFAULT_SOURCE / "oscopilot").is_dir(),
                     "initialize OS-Copilot and install [oscopilot]")
class NativeRuntimeTests(unittest.TestCase):
    def test_native_class_invocation_accepts_non_json_internal_values_without_metadata(self):
        native = load_upstream()
        executor = native.executor.__new__(native.executor)
        source = ("from dataclasses import dataclass\n"
                  "@dataclass\n"
                  "class Inspector:\n"
                  "    value: str\n"
                  "    def __call__(self):\n"
                  "        return {self.value}\n")
        with tempfile.TemporaryDirectory() as directory:
            executor.environment = NativeEnvironment(Path(directory), 30)
            with redirect_stdout(io.StringIO()):
                state = executor.execute_tool(source, "Inspector('native-evidence')()", "Python")
        self.assertIsNone(state.error, state.error)
        self.assertIn("native-evidence", state.result)
        self.assertIn("<return>", state.result)
        self.assertEqual(executor.environment.last_result["status"], "completed")

    def test_actual_native_invocation_errors_are_visible_to_the_repair_state(self):
        native = load_upstream()
        executor = native.executor.__new__(native.executor)
        with tempfile.TemporaryDirectory() as directory:
            executor.environment = NativeEnvironment(Path(directory), 30)
            with redirect_stdout(io.StringIO()):
                state = executor.execute_tool("def inspect():\n    raise ValueError('missing input')", "inspect()", "Python")
        self.assertIn("missing input", state.error)
        self.assertEqual(executor.environment.last_result["status"], "error")
