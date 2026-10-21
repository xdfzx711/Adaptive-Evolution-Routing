"""Synchronous Codex CLI Agent. Model/tool execution belongs to Codex itself."""

import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import tomllib
from time import monotonic, sleep
from typing import Literal

from pydantic import Field, model_validator

from aer_core.config.models import AgentSettings, NonBlank, NonNegativeInt, PositiveFloat, Settings
from aer_core.errors import ExecutionError, PluginConfigurationError, UnsupportedCapability
from aer_core.identifiers import timestamp_id
from aer_core.schemas.evolution import AgentCapabilities
from aer_core.schemas.execution import ExecutionResult
from aer_core.schemas.usage import ResourceUsage
from plugins.agents.execution import SessionRecord
from plugins.agents.model_selection import ModelId, ModelSelection, SUPPORTED_MODELS
from plugins.agents.output import PublicJsonOutput


class CodexConfig(Settings):
    command: list[NonBlank] = Field(default_factory=lambda: ["codex"])
    initial_model: ModelId = "gpt-5.6-sol"
    allowed_models: list[ModelId] = Field(default_factory=lambda: list(SUPPORTED_MODELS))
    workspace_root: NonBlank | None = None
    timeout_seconds: PositiveFloat = 300.0
    profile: NonBlank | None = None
    ignore_user_config: bool = True
    windows_sandbox: Literal["mxc", "elevated"] = "mxc"
    startup_retries: NonNegativeInt = 2

    @model_validator(mode="after")
    def valid_catalog(self):
        if not self.command:
            raise ValueError("command cannot be empty")
        if not self.allowed_models or len(set(self.allowed_models)) != len(self.allowed_models):
            raise ValueError("allowed_models must be nonempty and unique")
        if self.initial_model not in self.allowed_models:
            raise ValueError("initial_model must belong to allowed_models")
        if self.workspace_root is not None and not Path(self.workspace_root).is_absolute():
            raise ValueError("workspace_root must be an absolute path")
        if self.profile is not None and any(char in self.profile for char in ("/", "\\", ":")):
            raise ValueError("profile must be a name, not a path")
        return self


class CodexAgentSettings(AgentSettings):
    @model_validator(mode="after")
    def plugin_settings(self):
        CodexConfig.model_validate(self.config)
        if self.max_steps is not None:
            raise ValueError("Codex CLI has no ReAct max_steps setting; omit agent.max_steps")
        return self


def snapshot_config(config):
    settings = CodexConfig.model_validate(config.agent.config).model_dump(mode="json")
    settings.pop("initial_model")
    return settings


def initial_artifacts(config):
    settings = CodexConfig.model_validate(config.agent.config)
    return {"prompt": config.agent.initial_prompt.encode("utf-8"),
            "model": ModelSelection(model_id=settings.initial_model).model_dump_json(indent=2).encode("utf-8")}


def command_prefix(command):
    executable = shutil.which(command[0])
    if executable is None:
        raise ValueError("Codex command was not found; install Codex CLI or set agent.config.command")
    path = Path(executable)
    if os.name == "nt" and path.suffix.lower() in {".cmd", ".bat", ".ps1"}:
        # npm's Windows shim requires a shell; invoke its JS launcher directly.
        launcher = path.parent / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
        node = shutil.which("node")
        if not launcher.is_file() or node is None:
            raise ValueError("Use a native Codex executable or the standard npm Codex installation")
        return [node, str(launcher), *command[1:]]
    return [executable, *command[1:]]


def stop_process_tree(process):
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW, check=False)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.kill()


def retryable_startup_failure(stdout, stderr):
    """Only retry a known MXC bootstrap failure before any thread or turn.

    A partially executed task is an observation and must never be run again
    through this mechanism. Unknown/unparseable output is deliberately excluded.
    """
    message = stderr.lower()
    if not ("createprocesssecurityenvironment" in message and "0x80070003" in message
            and "failed to load agents.md" in message):
        return False
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            return False
        if not isinstance(event, dict) or event.get("type") != "error":
            return False
    return True


class CodexCliAgent:
    plugin_id = "codex_cli_agent"

    def __init__(self, config, initial_snapshot, artifacts, output_adapter=None):
        if config.plugin != self.plugin_id:
            raise ValueError("Agent config does not match its factory")
        self.settings = CodexConfig.model_validate(config.config)
        self.artifacts = artifacts
        self.output_adapter = output_adapter or PublicJsonOutput()
        self._snapshot = initial_snapshot
        self._controlled = {key: value for key, value in self.settings.model_dump(mode="json").items()
                            if key != "initial_model"}
        self.project_root = Path(__file__).resolve().parents[2]
        self.workspace_root = Path(self.settings.workspace_root or (Path(tempfile.gettempdir()) / "aer-codex-workspaces")).resolve()
        if self.workspace_root.is_relative_to(self.project_root):
            raise PluginConfigurationError("Codex workspace_root must be outside the AER project", stage="setup", plugin_id=self.plugin_id)
        try:
            self.command = command_prefix(self.settings.command)
            result = subprocess.run([*self.command, "--version"], capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=10, check=True,
                **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}))
            self.cli_version = result.stdout.strip()
            version = re.search(r"codex-cli (\d+)\.(\d+)\.(\d+)", self.cli_version)
            if version and tuple(map(int, version.groups())) < (0, 162, 1):
                raise ValueError("This adapter requires Codex CLI 0.162.1 or later")
        except (OSError, ValueError, subprocess.SubprocessError) as failure:
            raise PluginConfigurationError(f"Cannot initialize Codex CLI: {failure}", stage="setup", plugin_id=self.plugin_id) from failure
        self._validate_snapshot(initial_snapshot)

    def capabilities(self):
        return AgentCapabilities(supported_targets=["prompt", "model"], supported_artifact_kinds=["prompt", "model"],
            can_rebuild_session=True, snapshot_scope="controlled_config",
            feedback_state_fields=["agent_plugin", "execution_model", "termination_reason", "fresh_session", "seed_supported"],
            feedback_config_fields=["allowed_models"],
            feedback_tool_arguments={"command_execution": ["command"], "file_change": ["changes"],
                "web_search": ["query"], "mcp_tool_call": ["server", "tool", "arguments"]},
            feedback_tool_outputs={"command_execution": ["text", "exit_code"], "file_change": ["changes"],
                "web_search": ["query"], "mcp_tool_call": ["result"]})

    def _validate_snapshot(self, snapshot):
        if snapshot.agent_plugin_id != self.plugin_id or snapshot.model_dump(mode="json")["config"] != self._controlled:
            raise UnsupportedCapability("Codex Snapshot runtime settings differ from the assembled plugin", stage="setup", plugin_id=self.plugin_id)
        if any(kind not in snapshot.artifacts or snapshot.artifacts[kind].kind != kind for kind in ("prompt", "model")):
            raise UnsupportedCapability("Codex requires prompt and model artifacts", stage="setup", plugin_id=self.plugin_id)
        if set(snapshot.artifacts) - {"prompt", "model"}:
            raise UnsupportedCapability("Codex Snapshot contains unsupported components", stage="setup", plugin_id=self.plugin_id)
        selection = ModelSelection.model_validate_json(self.artifacts.get(snapshot.artifacts["model"]))
        if selection.model_id not in self.settings.allowed_models:
            raise UnsupportedCapability("Snapshot execution model is not allowed", stage="setup", plugin_id=self.plugin_id)
        prompt = self.artifacts.get(snapshot.artifacts["prompt"]).decode("utf-8")
        if not prompt.strip():
            raise UnsupportedCapability("Codex prompt cannot be blank", stage="setup", plugin_id=self.plugin_id)
        return selection.model_id, prompt

    def snapshot(self):
        return self._snapshot

    def restore(self, snapshot):
        self._validate_snapshot(snapshot)
        self._snapshot = snapshot

    def runtime_info(self):
        home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
        names = ["AGENTS.md", "AGENTS.override.md"]
        if not self.settings.ignore_user_config:
            names.append("config.toml")
        if self.settings.profile:
            names.append(self.settings.profile + ".config.toml")
        fingerprints = {name: hashlib.sha256((home / name).read_bytes()).hexdigest()
                        for name in names if (home / name).is_file()}
        return {"cli_version": self.cli_version, "command": self.command, "config_fingerprints": fingerprints,
                "windows_sandbox": self.settings.windows_sandbox if os.name == "nt" else None,
                "seed_supported": False, "model_calls_reported": False, "tool_calls_scope": "visible_cli_items"}

    def _check_permission_config(self):
        home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
        names = [] if self.settings.ignore_user_config else ["config.toml"]
        if self.settings.profile:
            names.append(self.settings.profile + ".config.toml")
        servers = set()
        for name in names:
            path = home / name
            if not path.is_file():
                continue
            data = tomllib.loads(path.read_text(encoding="utf-8"))
            if {"sandbox_mode", "sandbox_workspace_write"} & data.keys():
                raise ValueError("Codex legacy sandbox settings override task permissions; use "
                    "agent.config.ignore_user_config=true or a config without legacy sandbox settings")
            servers.update(data.get("mcp_servers", {}))
        return servers

    def _arguments(self, workspace, schema_path, model_id, prompt, run_ctx):
        servers = self._check_permission_config()
        args = [*self.command, "exec", "--json", "--ephemeral", "--skip-git-repo-check",
                "--color", "never", "--cd", str(workspace), "--model", model_id]
        if self.settings.ignore_user_config:
            args.append("--ignore-user-config")
        if self.settings.profile:
            args.extend(["--profile", self.settings.profile])
        # Deny grading sources and saved feedback even though workspace-write
        # alone normally permits reading outside the working directory.
        denied_paths = {self.project_root, Path(run_ctx.workspace_ref).resolve()}
        # RunContext can name a logical workspace whose parents do not exist.
        # MXC cannot initialize some such nested deny entries (0x80070003).
        # The project-wide denial already covers every descendant, including
        # future run directories, so only emit the outermost protected paths.
        denied = {str(path) for path in denied_paths
                  if not any(path != parent and path.is_relative_to(parent) for parent in denied_paths)}
        filesystem = '":root"="deny", ":minimal"="read", ":tmpdir"="deny", ":slash_tmp"="deny", '
        filesystem += ', '.join(json.dumps(path) + '="deny"' for path in sorted(denied))
        overrides = ["approval_policy=\"never\"", "default_permissions=\"aer_task\"",
            "permissions.aer_task.extends=\":workspace\"",
            "permissions.aer_task.filesystem={" + filesystem + "}",
            "features.multi_agent=false", "features.apps=false", "features.goals=false", "web_search=\"disabled\"",
            "developer_instructions=" + json.dumps(prompt, ensure_ascii=False)]
        if os.name == "nt":
            # The standalone CLI can otherwise select unelevated, which cannot
            # enforce this Agent's split read/write filesystem permissions.
            overrides.append("windows.sandbox=" + json.dumps(self.settings.windows_sandbox))
        overrides.extend("mcp_servers." + json.dumps(name) + ".enabled=false" for name in sorted(servers))
        for value in overrides:
            args.extend(["--config", value])
        if schema_path is not None:
            args.extend(["--output-schema", str(schema_path)])
        return [*args, "-"]

    def run(self, task, snapshot, run_ctx):
        record = SessionRecord()
        output, error, status, termination = {}, None, "completed", "finish"
        final, turn_usage, process, workspace = None, None, None, None
        model_id = None
        tools, completed_tools = set(), set()
        startup_attempts = 0
        session_started, turn_started = False, False
        try:
            model_id, prompt = self._validate_snapshot(snapshot)
            self.workspace_root.mkdir(parents=True, exist_ok=True)
            workspace = Path(tempfile.mkdtemp(prefix=timestamp_id("task") + "-", dir=self.workspace_root))
            schema = self.output_adapter.schema(task)
            schema_path = workspace / "output-schema.json" if schema is not None else None
            if schema_path:
                schema_path.write_text(json.dumps(schema, ensure_ascii=False, indent=2), encoding="utf-8")
            public_input = task.model_dump_json(indent=2)
            args = self._arguments(workspace, schema_path, model_id, prompt, run_ctx)
            timed_out = False
            deadline = record.start + self.settings.timeout_seconds
            for attempt in range(1, self.settings.startup_retries + 2):
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise ExecutionError("Codex execution timed out during startup", stage=run_ctx.stage)
                startup_attempts = attempt
                record.event("model_request", {"request": {"model_id": model_id, "messages": [
                    {"role": "developer", "content": prompt}, {"role": "user", "content": public_input}]},
                    "runtime": self.runtime_info(), "workspace": str(workspace), "startup_attempt": startup_attempts})
                process = subprocess.Popen(args, cwd=workspace, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                    **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}))
                try:
                    stdout, stderr = process.communicate(public_input, timeout=remaining)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    stop_process_tree(process)
                    stdout, stderr = process.communicate(timeout=10)
                if (not timed_out and process.returncode != 0 and os.name == "nt"
                    and startup_attempts <= self.settings.startup_retries
                    and retryable_startup_failure(stdout, stderr)):
                    record.event("startup_retry", {"attempt": startup_attempts,
                        "reason": "MXC failed before thread creation: CreateProcessSecurityEnvironment 0x80070003",
                        "model_call_started": False})
                    sleep(min(0.2, max(0, deadline - monotonic())))
                    continue
                break
            failed, turn_completed = False, False
            for line in stdout.splitlines():
                if not line.strip():
                    continue
                event = json.loads(line)
                kind, item = event.get("type"), event.get("item", {})
                if kind == "thread.started":
                    session_started = True
                elif kind == "turn.started":
                    turn_started = True
                elif kind == "turn.completed":
                    turn_completed = True
                    turn_usage = event.get("usage")
                elif kind == "turn.failed":
                    failed = True
                    record.event("execution_error", {"message": str(event.get("error", event.get("message", "Codex failed")))})
                elif kind == "error":
                    record.event("runtime_notice", {"message": str(event.get("message", event.get("error", "Codex notice")))})
                elif kind in {"item.started", "item.completed"}:
                    item_type = item.get("type")
                    if item_type == "agent_message" and kind == "item.completed":
                        final = item.get("text")
                        record.event("model_response", {"content": final, "finish_reason": "stop"})
                    elif item_type in {"command_execution", "file_change", "web_search", "mcp_tool_call"}:
                        identity = item["id"]
                        if identity not in tools:
                            tools.add(identity)
                            arguments = {key: item[key] for key in ("command", "changes", "query", "server", "tool", "arguments") if key in item}
                            record.event("tool_call", {"call_id": identity, "name": item_type, "arguments": arguments})
                        if kind == "item.completed" and identity not in completed_tools:
                            completed_tools.add(identity)
                            result = {key: item[key] for key in ("exit_code", "changes", "query", "result") if key in item}
                            if "aggregated_output" in item:
                                result["text"] = item["aggregated_output"]
                            tool_error = item.get("error")
                            if not tool_error and item.get("exit_code") not in (None, 0):
                                tool_error = f"Command exited with status {item['exit_code']}"
                            record.event("tool_result", {"call_id": identity, "name": item_type,
                                "status": "error" if tool_error or item.get("status") == "failed" else "completed", "output": result,
                                "error": {"code": "tool_error", "message": str(tool_error)} if tool_error else None})
            if timed_out or process.returncode != 0 or failed or not turn_completed or final is None:
                phase = "model turn" if turn_started else "session startup"
                detail = f"timeout during {phase}; {stderr[-2000:]}" if timed_out else f"exit={process.returncode}; {stderr[-2000:]}"
                raise ExecutionError(f"Codex execution failed after {startup_attempts} startup attempt(s): " + detail,
                                     stage=run_ctx.stage, plugin_id=self.plugin_id)
            try:
                output = self.output_adapter.parse(final)
            except ValueError as failure:
                termination = "invalid_output_format"
                record.event("output_parse_error", {"message": str(failure), "raw_output": final})
            record.event("finish", {"output": output, "termination_reason": termination})
        except Exception as failure:
            status, termination = "error", "execution_error"
            error = ExecutionError(str(failure) or type(failure).__name__, stage=run_ctx.stage,
                operation_id=run_ctx.operation_id, plugin_id=self.plugin_id).to_info()
            record.event("execution_error", {"message": error.message})
        finally:
            if process is not None and process.poll() is None:
                stop_process_tree(process)
                process.communicate(timeout=10)
        incoming = turn_usage.get("input_tokens") if isinstance(turn_usage, dict) else None
        outgoing = turn_usage.get("output_tokens") if isinstance(turn_usage, dict) else None
        if process is None:
            incoming, outgoing = 0, 0
        known = all(type(value) is int and value >= 0 for value in (incoming, outgoing))
        usage = ResourceUsage(input_tokens=incoming if known else None, output_tokens=outgoing if known else None,
            total_tokens=incoming + outgoing if known else None, model_calls=None if process is not None else 0,
            tool_calls=len(tools), elapsed_seconds=monotonic() - record.start, usage_complete=known,
            usage_sources=[] if process is None else ["provider_reported" if known else "unknown"])
        if error is not None:
            error = error.replace(usage=usage)
        return ExecutionResult(task_id=task.task_id, agent_version_id=snapshot.version_id,
            operation_id=run_ctx.operation_id, output=output, trajectory=record.events, usage=usage,
            status=status, error=error, state_summary={"agent_plugin": self.plugin_id, "execution_model": model_id,
                "termination_reason": termination, "fresh_session": True, "seed_supported": False,
                "workspace": str(workspace) if workspace else None, "model_calls_scope": "not_reported",
                "tool_calls_scope": "visible_cli_items", "startup_attempts": startup_attempts,
                "session_started": session_started, "turn_started": turn_started})
