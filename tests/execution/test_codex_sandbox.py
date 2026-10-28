"""Opt-in native sandbox checks; no model or benchmark inference."""

import json
import os
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path

from plugins.agents.codex import command_prefix
from tests.execution.helpers import context
from tests.execution.test_codex import fixture


@unittest.skipUnless(os.name == "nt" and os.environ.get("AER_TEST_CODEX_SANDBOX") == "1",
                     "set AER_TEST_CODEX_SANDBOX=1 for local Windows MXC checks")
class NativeCodexSandboxTests(unittest.TestCase):
    def test_workspace_commands_work_and_other_task_data_stays_denied(self):
        for internal in (False, True):
            with self.subTest(internal_optimizer_layout=internal):
                self.check_workspace(internal)

    def check_workspace(self, internal):
        with tempfile.TemporaryDirectory(prefix="aer-sandbox-check-") as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            sample = workspace / "sample.py"
            sample.write_text("aer_probe_value = 1\n", encoding="utf-8")
            private = root / "other-task.txt"
            private.write_text("other task data", encoding="utf-8")
            agent, _, _ = fixture(root)
            subprocess.run([agent.tool_executables["git"], "init", str(workspace)],
                           check=True, capture_output=True)
            run_context = context(workspace_ref=str(root)) if internal else context()
            arguments = agent._arguments(workspace, None, "gpt-5.6-sol", "probe", run_context)
            overrides = [arguments[i + 1] for i, value in enumerate(arguments[:-1]) if value == "--config"]
            args = [*command_prefix(["codex"]), "sandbox", "--include-managed-config",
                    "--permission-profile", "aer_task", "--cd", str(workspace)]
            env = dict(os.environ)
            for value in overrides:
                if value.startswith(("permissions.", "windows.", "default_permissions=")):
                    args.extend(["--config", value])
                if value.startswith("shell_environment_policy.set."):
                    env.update(tomllib.loads(value)["shell_environment_policy"]["set"])
            quote = lambda path: "'" + str(path).replace("'", "''") + "'"
            script = (
                "$ErrorActionPreference='Stop'; "
                f"if ((Get-Location).Path -ne {quote(workspace)}) {{throw 'wrong working directory'}}; "
                f"Get-Content -LiteralPath {quote(sample)} | Out-Null; "
                f"& {quote(agent.tool_executables['git'])} -C {quote(workspace)} status --short; "
                "if ($LASTEXITCODE -ne 0) {throw 'git failed'}; "
                f"& {quote(agent.tool_executables['rg'])} -q aer_probe_value {quote(sample)}; "
                "if ($LASTEXITCODE -ne 0) {throw 'rg failed'}; "
                f"Set-Content -LiteralPath {quote(workspace / 'written.txt')} -Value 'ok'; "
            )
            for path in (private, agent.project_root / "README.md"):
                script += (
                    "$blocked=$false; "
                    f"try {{Get-Content -LiteralPath {quote(path)} | Out-Null}} catch {{$blocked=$true}}; "
                    "if (-not $blocked) {throw 'outside workspace read was allowed'}; "
                )
            script += (
                "$blocked=$false; "
                f"try {{Set-Content -LiteralPath {quote(private)} -Value 'bad'}} catch {{$blocked=$true}}; "
                "if (-not $blocked) {throw 'outside workspace write was allowed'}; 'AER_SANDBOX_OK'"
            )
            result = subprocess.run([*args, "--", "powershell.exe", "-NoProfile", "-NonInteractive",
                                     "-Command", script], env=env, capture_output=True, encoding="utf-8",
                                    errors="replace", timeout=40)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("AER_SANDBOX_OK", result.stdout)
            self.assertEqual(private.read_text(encoding="utf-8"), "other task data")
            self.assertEqual((workspace / "written.txt").read_text().strip(), "ok")
