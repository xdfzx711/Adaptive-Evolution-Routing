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

from aer_core.config.models import AgentSettings, ComponentSettings, NonBlank, NonNegativeInt, PositiveFloat
from aer_core.errors import ExecutionError, PluginConfigurationError, UnsupportedCapability
from aer_core.identifiers import timestamp_id
from aer_core.schemas.evolution import AgentCapabilities
from aer_core.schemas.execution import ExecutionResult
from aer_core.schemas.usage import ResourceUsage
from plugins.agents.execution import SessionRecord
from plugins.agents.model_selection import CodexModelAdapter, CodexModelSpace, ModelId, ModelSelection, ReasoningEffort
from plugins.agents.tool_selection import CodexToolAdapter
from plugins.tools.catalog import ToolingConfig
from plugins.tool_selection import ToolSelection
from plugins.tools.generated.bundle import ToolBundle, generated_tools
from plugins.agents.output import PublicJsonOutput
from plugins.workspace.local import EmptyWorkspace
from plugins.workspace.collectors import JsonResultCollector
from plugins.agents.codex_runtime import native_tools, task_environment, error_message
from plugins.agents.codex_process import capture_cli
from plugins.skills.library import SkillsConfig, initial_skill_artifacts
from plugins.skills.runtime import SkillRuntime, STATE_FIELDS, RUNTIME_VERSION
from plugins.skills.codex import CodexSkillSession, repository_skill_overrides
from aer_core.schemas.skills import SkillDeliveryCapabilities
from aer_core.usage import sum_usage
from plugins.memory.library import MemoryConfig, initial_memory_artifacts
from plugins.memory.runtime import MemoryRuntime, STATE_FIELDS as MEMORY_STATE_FIELDS


class CodexConfig(CodexModelSpace):
    command: list[NonBlank] = Field(default_factory=lambda: ["codex"])
    initial_model: ModelId = "gpt-6.1-sol"
    initial_reasoning_effort: ReasoningEffort | None = None
    workspace_root: NonBlank | None = None
    timeout_seconds: PositiveFloat = 300.0
    profile: NonBlank | None = None
    ignore_user_config: bool = True
    windows_sandbox: Literal["mxc", "elevated"] = "mxc"
    startup_retries: NonNegativeInt = 2
    workspace: ComponentSettings = Field(default_factory=lambda: ComponentSettings(plugin="empty_workspace"))
    collector: ComponentSettings = Field(default_factory=lambda: ComponentSettings(plugin="json_result_collector"))
    tooling: ToolingConfig | None = None
    initial_tools: list[NonBlank] | None = None
    skills: SkillsConfig | None = None
    memory: MemoryConfig | None = None

    @model_validator(mode="after")
    def valid_catalog(self):
        if self.skills is not None and self.skills.mode != "on_demand":
            raise ValueError("Codex skill delivery requires mode=on_demand")
        if not self.command:
            raise ValueError("command cannot be empty")
        if self.initial_model not in self.allowed_models:
            raise ValueError("initial_model must belong to allowed_models")
        if ModelSelection(model_id=self.initial_model, reasoning_effort=self.initial_reasoning_effort) not in self.selections():
            raise ValueError("initial model/reasoning combination must belong to the configured catalog")
        if self.workspace_root is not None and not Path(self.workspace_root).is_absolute():
            raise ValueError("workspace_root must be an absolute path")
        if self.profile is not None and any(char in self.profile for char in ("/", "\\", ":")):
            raise ValueError("profile must be a name, not a path")
        if self.initial_tools is not None:
            selected = ToolSelection(enabled_tools=self.initial_tools)
            if self.tooling is None or not set(selected.enabled_tools) <= self.tooling.catalog.keys():
                raise ValueError("initial_tools requires tooling and identifiers from its catalog")
        return self


class CodexAgentSettings(AgentSettings):
    @model_validator(mode="after")
    def plugin_settings(self):
        CodexConfig.model_validate(self.config)
        if self.max_steps is not None:
            raise ValueError("Codex CLI has no ReAct max_steps setting; omit agent.max_steps")
        return self


def snapshot_config(config):
    return controlled_config(CodexConfig.model_validate(config.agent.config))


def controlled_config(settings):
    values = settings.model_dump(mode="json", exclude={"initial_model", "initial_reasoning_effort", "initial_tools"})
    if settings.memory is None:
        values.pop("memory")
    else:
        values["memory"] = settings.memory.controlled()
    if settings.skills is None:
        values.pop("skills")
    else:
        values["skills"] = settings.skills.controlled()
    # Keep old Snapshot identities and frozen-config comparisons unchanged when not opted in.
    if not values["allowed_reasoning_efforts"]:
        values.pop("allowed_reasoning_efforts")
    if settings.tooling is None:
        values.pop("tooling")
    else:
        if settings.tooling.generated_tools is None:
            values["tooling"].pop("generated_tools", None)
        elif settings.tooling.generated_tools.enabled:
            values["generated_tools_enabled"] = True
        values["tool_catalog"] = settings.tooling.public_catalog()
    return values


def initial_artifacts(config):
    settings = CodexConfig.model_validate(config.agent.config)
    contents = {"prompt": config.agent.initial_prompt.encode("utf-8"),
            "model": CodexModelAdapter().encode(ModelSelection(model_id=settings.initial_model,
                reasoning_effort=settings.initial_reasoning_effort))}
    if settings.tooling is not None:
        selection_type = ToolBundle if settings.tooling.generated_tools and settings.tooling.generated_tools.enabled else ToolSelection
        contents["tool"] = CodexToolAdapter().encode(selection_type(enabled_tools=settings.initial_tools or []))
    contents.update(initial_skill_artifacts(settings.skills))
    contents.update(initial_memory_artifacts(settings.memory))
    return contents


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

    def __init__(self, config, initial_snapshot, artifacts, output_adapter=None, *, workspace_provider=None, result_collector=None,
                 tool_adapter=None, launch_adapter=None):
        if config.plugin != self.plugin_id:
            raise ValueError("Agent config does not match its factory")
        self.settings = CodexConfig.model_validate(config.config)
        if workspace_provider is None and self.settings.workspace.plugin != "empty_workspace":
            raise PluginConfigurationError("Selected workspace plugin must be injected by startup", stage="setup", plugin_id=self.plugin_id)
        if result_collector is None and self.settings.collector.plugin != "json_result_collector":
            raise PluginConfigurationError("Selected result collector must be injected by startup", stage="setup", plugin_id=self.plugin_id)
        self.artifacts = artifacts
        self.memory = MemoryRuntime(self.settings.memory, artifacts) if self.settings.memory is not None else None
        self.model_adapter = CodexModelAdapter()
        self.tool_adapter = tool_adapter or CodexToolAdapter()
        self.launch_adapter = launch_adapter
        self.output_adapter = output_adapter or PublicJsonOutput()
        self.workspace_provider = workspace_provider or EmptyWorkspace()
        self.result_collector = result_collector or JsonResultCollector(self.output_adapter)
        self._snapshot = initial_snapshot
        self._controlled = controlled_config(self.settings)
        self.project_root = Path(__file__).resolve().parents[2]
        self.tool_executables, self.tool_read_roots = native_tools(self.project_root)
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
        # Registration describes plugin support. TargetRegistry separately requires
        # a configured tool artifact before exposing this target to a Router.
        components = ["prompt", "model", "tool", *(["memory"] if self.memory else [])]
        return AgentCapabilities(supported_targets=["prompt", "model", "tool", "skill", "memory"],
            supported_artifact_kinds=[*components, *(["skill"] if self.settings.skills is not None else [])],
            can_rebuild_session=True, snapshot_scope="controlled_config",
            feedback_state_fields=["agent_plugin", "execution_model", "execution_reasoning_effort", "execution_platform", "execution_shell", "termination_reason", "fresh_session", "seed_supported",
                                   "result_kind", "evaluation_mode", "selected_tools", "available_tools", "tool_setup_status", "tool_setup_error", "tool_invocations", "generated_tool_catalog",
                                   "tool_discovered", "tool_invocation_count", "tool_invocations_omitted", "tool_metrics", *(STATE_FIELDS if self.settings.skills is not None else ()), *(MEMORY_STATE_FIELDS if self.memory else ())],
            feedback_config_fields=["allowed_models", "allowed_reasoning_efforts", "tool_catalog", "generated_tools_enabled"],
            feedback_tool_arguments={"command_execution": ["command"], "file_change": ["changes"],
                "web_search": ["query"], "mcp_tool_call": ["server", "tool", "arguments"]},
            feedback_tool_outputs={"command_execution": ["text", "exit_code"], "file_change": ["changes"],
                "web_search": ["query"], "mcp_tool_call": ["result"]},
            skill_delivery=SkillDeliveryCapabilities(transport="mcp", discovery="catalog", observes_reads=True)
                if self.settings.skills is not None else None)

    def _validate_snapshot(self, snapshot):
        configuration = dict(snapshot.model_dump(mode="json")["config"])
        if configuration.get("allowed_reasoning_efforts") == {}:
            configuration.pop("allowed_reasoning_efforts")
        if configuration.get("initial_reasoning_effort", "absent") is None:
            configuration.pop("initial_reasoning_effort")
        if snapshot.agent_plugin_id != self.plugin_id or configuration != self._controlled:
            raise UnsupportedCapability("Codex Snapshot runtime settings differ from the assembled plugin", stage="setup", plugin_id=self.plugin_id)
        required = {"prompt", "model", *({"tool"} if self.settings.tooling is not None else set()),
                    *({"skill"} if self.settings.skills is not None else set()), *({"memory"} if self.memory else set())}
        if any(kind not in snapshot.artifacts or snapshot.artifacts[kind].kind != kind for kind in required):
            raise UnsupportedCapability("Codex requires artifacts for all configured components", stage="setup", plugin_id=self.plugin_id)
        if set(snapshot.artifacts) - required:
            raise UnsupportedCapability("Codex Snapshot contains unsupported components", stage="setup", plugin_id=self.plugin_id)
        try:
            selection = self.model_adapter.read(snapshot, self.artifacts)
            if self.settings.tooling is not None:
                self.tool_adapter.read(snapshot, self.artifacts)
            if self.settings.skills is not None:
                SkillRuntime.from_snapshot(snapshot, self.artifacts, "setup")
            if self.memory:
                self.memory.validate(snapshot)
        except ValueError as failure:
            raise UnsupportedCapability("Snapshot model, reasoning, tools, or skills are invalid", stage="setup", plugin_id=self.plugin_id) from failure
        prompt = self.artifacts.get(snapshot.artifacts["prompt"]).decode("utf-8")
        if not prompt.strip():
            raise UnsupportedCapability("Codex prompt cannot be blank", stage="setup", plugin_id=self.plugin_id)
        return selection, prompt

    def snapshot(self):
        return self._snapshot

    def restore(self, snapshot):
        self._validate_snapshot(snapshot)
        self._snapshot = snapshot

    def runtime_info(self):
        from plugins.tools.generated.runtime import runtime_identity
        home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
        names = ["AGENTS.md", "AGENTS.override.md"]
        if not self.settings.ignore_user_config:
            names.append("config.toml")
        if self.settings.profile:
            names.append(self.settings.profile + ".config.toml")
        fingerprints = {name: hashlib.sha256((home / name).read_bytes()).hexdigest()
                        for name in names if (home / name).is_file()}
        return {"cli_version": self.cli_version, "command": self.command, "config_fingerprints": fingerprints,
                **({"memory_runtime": self.memory.runtime_info()} if self.memory else {}),
                **({"skill_runtime_version": RUNTIME_VERSION, "skill_delivery": "mcp",
                    "skip_host_skill_discovery": True, "plugins_enabled": False} if self.settings.skills is not None else {}),
                "task_permissions_version": "2",
                "tool_executables": self.tool_executables,
                "tool_read_roots": [str(path) for path in self.tool_read_roots],
                "windows_sandbox": self.settings.windows_sandbox if os.name == "nt" else None,
                "seed_supported": False, "model_calls_reported": False, "tool_calls_scope": "visible_cli_items",
                **({"generated_tool_runtime": runtime_identity(self.settings.tooling.generated_tools.python_executable)}
                   if self.settings.tooling and self.settings.tooling.generated_tools else {}),
                **({"tool_adapter_version": "3.0.0" if self.settings.tooling.generated_tools else "1.0.0",
                    "tool_catalog": self.settings.tooling.public_catalog()}
                   if self.settings.tooling is not None else {})}

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

    def _arguments(self, workspace, schema_path, model_id, prompt, run_ctx, tool_session=None, skill_session=None):
        selection = model_id if isinstance(model_id, ModelSelection) else ModelSelection(model_id=model_id)
        servers = self._check_permission_config()
        args = [*self.command, "exec", "--json", "--ephemeral", "--skip-git-repo-check",
                "--color", "never", "--cd", str(workspace), "--model", selection.model_id]
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
        # Internal optimizers keep private data and task workspaces under one
        # temporary parent. An explicit ancestor deny masks the workspace grant
        # in MXC. The :root deny below already protects every sibling; retain
        # explicit denials only where they don't mask the task directory.
        denied_roots = {path for path in denied_paths
                        if path == workspace or not workspace.is_relative_to(path)}
        denied = {str(path) for path in denied_roots
                  if not any(path != parent and path.is_relative_to(parent) for parent in denied_roots)}
        # Denying :tmpdir also denies directory handles for AER's workspaces
        # under TEMP in MXC. Grant only this workspace from a read-only parent,
        # without inheriting :workspace's shared temporary-directory grants.
        filesystem = '":root"="deny", ":minimal"="read", '
        filesystem += json.dumps(str(workspace)) + '="write", '
        filesystem += ', '.join(json.dumps(path) + '="deny"' for path in sorted(denied))
        # Nonstandard Windows Git/rg installs are absent from :minimal. Permit
        # only their runtime folders, keeping task data and host sources denied.
        for path in self.tool_read_roots:
            if not any(path.is_relative_to(parent) or parent.is_relative_to(path) for parent in denied_paths):
                filesystem += ', ' + json.dumps(str(path)) + '="read"'
        overrides = ["approval_policy=\"never\"", "default_permissions=\"aer_task\"",
            "permissions.aer_task.extends=\":read-only\"",
            "permissions.aer_task.filesystem={" + filesystem + "}",
            "features.multi_agent=false", "features.apps=false", "features.goals=false", "web_search=\"disabled\"",
            "developer_instructions=" + json.dumps(prompt, ensure_ascii=False)]
        if os.name == "nt":
            # The standalone CLI can otherwise select unelevated, which cannot
            # enforce this Agent's split read/write filesystem permissions.
            overrides.append("windows.sandbox=" + json.dumps(self.settings.windows_sandbox))
            # MXC's effective user differs from the repository's host owner.
            # Trust this task repository, without modifying host Git settings.
            overrides.extend([
                'shell_environment_policy.set.GIT_CONFIG_GLOBAL="NUL"',
                'shell_environment_policy.set.GIT_CONFIG_NOSYSTEM="1"',
                'shell_environment_policy.set.GIT_CONFIG_COUNT="1"',
                'shell_environment_policy.set.GIT_CONFIG_KEY_0="safe.directory"',
                'shell_environment_policy.set.GIT_CONFIG_VALUE_0=' + json.dumps(workspace.as_posix()),
                'shell_environment_policy.set.GIT_TERMINAL_PROMPT="0"',
            ])
        if selection.reasoning_effort is not None:
            overrides.append("model_reasoning_effort=" + json.dumps(selection.reasoning_effort))
        overrides.extend("mcp_servers." + json.dumps(name) + ".enabled=false" for name in sorted(servers)
                         if not (name == "aer_tools" and tool_session is not None and tool_session.selection.enabled_tools)
                         and not (name == "aer_skills" and skill_session is not None))
        if tool_session is not None:
            overrides.extend(tool_session.overrides)
        if skill_session is not None:
            overrides.extend(["features.skip_host_skill_discovery=true", "features.plugins=false",
                              repository_skill_overrides(workspace), *skill_session.overrides])
        for value in overrides:
            args.extend(["--config", value])
        if schema_path is not None:
            args.extend(["--output-schema", str(schema_path)])
        return [*args, "-"]

    def run(self, task, snapshot, run_ctx):
        record = SessionRecord()
        output, error, status, termination = {}, None, "completed", "finish"
        final, turn_usage, process, workspace = None, None, None, None
        model_id, selection = None, None
        tools, completed_tools = set(), set()
        startup_attempts = 0
        session_started, turn_started = False, False
        handle, collected, terminal_shutdown = None, None, False
        tool_session, tool_report, selected_tools = None, None, None
        skills, skill_session, skill_report = None, None, None
        launch_session, launch_report = None, None
        memory_state = {}
        try:
            selection, prompt = self._validate_snapshot(snapshot)
            if self.memory:
                injected, memory_state = self.memory.retrieve(task, snapshot, run_ctx, record)
                prompt += injected
            model_id = selection.model_id
            if self.settings.tooling is not None:
                selected_tools = self.tool_adapter.read(snapshot, self.artifacts)
            workspace_root = Path(run_ctx.execution_workspace_root).resolve() if run_ctx.execution_workspace_root else self.workspace_root
            if workspace_root.is_relative_to(self.project_root):
                raise ExecutionError("Execution workspace must be outside the AER project", stage=run_ctx.stage)
            workspace_root.mkdir(parents=True, exist_ok=True)
            workspace = Path(tempfile.mkdtemp(prefix=timestamp_id("task") + "-", dir=workspace_root))
            handle = self.workspace_provider.prepare(task, workspace, run_ctx)
            workspace = handle.path
            schema = self.result_collector.schema(task)
            schema_path = workspace / "output-schema.json" if schema is not None else None
            if schema_path:
                schema_path.write_text(json.dumps(schema, ensure_ascii=False, indent=2), encoding="utf-8")
            public_data = task.model_dump(mode="json")
            public_data["execution_environment"] = task_environment(workspace, self.tool_executables)
            public_input = json.dumps(public_data, ensure_ascii=False, indent=2)
            if selected_tools is not None:
                tool_session = self.tool_adapter.prepare(selected_tools, self.settings.tooling, workspace, self.project_root)
            if self.settings.skills is not None:
                skills = SkillRuntime.from_snapshot(snapshot, self.artifacts, run_ctx.operation_id)
                skill_session = CodexSkillSession(skills)
                prompt += skills.instructions()
            args = (self._arguments(workspace, schema_path, selection, prompt, run_ctx, tool_session, skill_session)
                    if skill_session else self._arguments(workspace, schema_path, selection, prompt, run_ctx, tool_session))
            if self.launch_adapter is not None:
                if self.settings.skills is not None:
                    raise ValueError("External launch adapter cannot be combined with AER skill delivery")
                launch_session = self.launch_adapter.prepare(workspace, run_ctx)
                args = launch_session.arguments(args)
            timed_out = False
            deadline = record.start + min(self.settings.timeout_seconds, run_ctx.execution_timeout_seconds or self.settings.timeout_seconds)
            for attempt in range(1, self.settings.startup_retries + 2):
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise ExecutionError("Codex execution timed out during startup", stage=run_ctx.stage)
                startup_attempts = attempt
                record.event("model_request", {"request": {"model_id": model_id,
                    "reasoning_effort": selection.reasoning_effort, "messages": [
                    {"role": "developer", "content": prompt}, {"role": "user", "content": public_input}]},
                    "runtime": self.runtime_info(), "workspace": str(workspace), "startup_attempt": startup_attempts})
                if tool_session is not None and tool_session.report_path is not None:
                    tool_session.report_path.unlink(missing_ok=True)
                if skill_session is not None:
                    skill_session.report_path.unlink(missing_ok=True)
                process = subprocess.Popen(args, cwd=workspace, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                    **({"env": launch_session.environment} if launch_session is not None else {}),
                    **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}))
                captured = capture_cli(process, public_input, remaining, stop_process_tree)
                stdout, stderr = captured.stdout, captured.stderr
                timed_out, terminal_shutdown = captured.timed_out, captured.terminal_shutdown
                if (not timed_out and process.returncode != 0 and os.name == "nt"
                    and startup_attempts <= self.settings.startup_retries
                    and retryable_startup_failure(stdout, stderr)):
                    record.event("startup_retry", {"attempt": startup_attempts,
                        "reason": "MXC failed before thread creation: CreateProcessSecurityEnvironment 0x80070003",
                        "model_call_started": False})
                    sleep(min(0.2, max(0, deadline - monotonic())))
                    continue
                break
            failed, turn_completed, cli_errors = False, False, []
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
                    message = error_message(event.get("error", event.get("message", "Codex failed")))
                    cli_errors.append(message)
                    record.event("execution_error", {"message": message})
                elif kind == "error":
                    message = error_message(event.get("message", event.get("error", "Codex notice")))
                    cli_errors.append(message)
                    record.event("runtime_notice", {"message": message})
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
                            if not tool_error and item_type == "mcp_tool_call" and isinstance(item.get("result"), dict) and item["result"].get("isError"):
                                tool_error = "MCP tool reported an error"
                            if not tool_error and item.get("exit_code") not in (None, 0):
                                tool_error = f"Command exited with status {item['exit_code']}"
                            record.event("tool_result", {"call_id": identity, "name": item_type,
                                "status": "error" if tool_error or item.get("status") == "failed" else "completed", "output": result,
                                "error": {"code": "tool_error", "message": str(tool_error)} if tool_error else None})
            if tool_session is not None:
                tool_report = tool_session.report()
                record.event("tool_setup", {"selected_tools": list(selected_tools.enabled_tools), **tool_report})
            if skill_session is not None:
                skill_report = skill_session.report()
                record.event("skill_setup", skill_report)
            if timed_out or (process.returncode != 0 and not terminal_shutdown) or failed or not turn_completed or final is None:
                phase = "model turn" if turn_started else "session startup"
                detail = f"timeout during {phase}; {stderr[-2000:]}" if timed_out else f"exit={process.returncode}; {stderr[-2000:]}"
                if cli_errors:
                    detail += "; " + cli_errors[-1][-2000:]
                raise ExecutionError(f"Codex execution failed after {startup_attempts} startup attempt(s): " + detail,
                                     stage=run_ctx.stage, plugin_id=self.plugin_id)
            if tool_report is not None and (tool_report["status"] != "ready" or not tool_report["discovered"]
                    or tool_report["available_tools"] != list(selected_tools.enabled_tools)):
                raise ExecutionError("Selected tools were not available to Codex: " + str(tool_report["error"] or tool_report["status"]),
                                     stage=run_ctx.stage, plugin_id=self.plugin_id)
            if skill_report is not None and (skill_report["status"] != "ready" or not skill_report["discovered"]):
                raise ExecutionError("Skill service was not available to Codex", stage=run_ctx.stage, plugin_id=self.plugin_id)
            if launch_session is not None:
                # Remove external runtime files before a patch collector sees them.
                launch_report = launch_session.finish()
            try:
                collected = self.result_collector.collect(task, handle, final, run_ctx)
                output = collected.model_dump(mode="json")["output"]
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
            if launch_session is not None and launch_report is None:
                try:
                    launch_report = launch_session.finish()
                except Exception as failure:
                    record.event("launch_cleanup_error", {"message": str(failure)})
            if tool_session is not None:
                if tool_report is None:
                    tool_report = tool_session.report()
                try:
                    tool_session.close()
                except OSError as failure:
                    record.event("tool_cleanup_error", {"message": type(failure).__name__})
            if skill_session is not None:
                try:
                    if skill_report is None:
                        skill_report = skill_session.report()
                except (OSError, ValueError, KeyError) as failure:
                    record.event("skill_report_error", {"message": str(failure)})
                finally:
                    try:
                        skill_session.close()
                    except OSError as failure:
                        record.event("skill_cleanup_error", {"message": type(failure).__name__})
            if handle is not None:
                try:
                    self.workspace_provider.release(handle)
                except Exception as failure:
                    record.event("workspace_release_error", {"message": str(failure)})
        incoming = turn_usage.get("input_tokens") if isinstance(turn_usage, dict) else None
        outgoing = turn_usage.get("output_tokens") if isinstance(turn_usage, dict) else None
        if process is None:
            incoming, outgoing = 0, 0
        known = all(type(value) is int and value >= 0 for value in (incoming, outgoing))
        usage = ResourceUsage(input_tokens=incoming if known else None, output_tokens=outgoing if known else None,
            total_tokens=incoming + outgoing if known else None, model_calls=None if process is not None else 0,
            tool_calls=len(tools), elapsed_seconds=monotonic() - record.start, usage_complete=known,
            usage_sources=[] if process is None else ["provider_reported" if known else "unknown"])
        if self.memory:
            usage = sum_usage([usage, record.usage()]).replace(elapsed_seconds=monotonic() - record.start)
        if error is not None:
            error = error.replace(usage=usage)
        return ExecutionResult(task_id=task.task_id, agent_version_id=snapshot.version_id,
            operation_id=run_ctx.operation_id, output=output, trajectory=record.events, usage=usage,
            status=status, error=error, state_summary={"agent_plugin": self.plugin_id, "execution_model": model_id, **memory_state,
                "execution_platform": os.name, "execution_shell": "powershell" if os.name == "nt" else "native",
                "execution_reasoning_effort": selection.reasoning_effort if selection else None,
                "termination_reason": termination, "fresh_session": True, "seed_supported": False,
                "workspace": str(workspace) if workspace else None, "model_calls_scope": "not_reported",
                "tool_calls_scope": "visible_cli_items", "startup_attempts": startup_attempts,
                "session_started": session_started, "turn_started": turn_started,
                "shutdown_after_terminal_event": terminal_shutdown,
                **({"external_runtime": launch_report} if launch_report is not None else {}),
                **(skills.summary("mcp", setup_status=skill_report["status"] if skill_report else "error",
                    discovered=skill_report.get("discovered") if skill_report else None) if skills else {}),
                **({"selected_tools": list(selected_tools.enabled_tools),
                    **({"generated_tool_catalog": {name: tool.descriptor().model_dump(mode="json")
                        for name, tool in generated_tools(selected_tools).items()}}
                       if self.settings.tooling.generated_tools else {}),
                    "available_tools": tool_report["available_tools"] if tool_report else [],
                    "tool_setup_status": tool_report["status"] if tool_report else "not_started",
                    "tool_setup_error": tool_report["error"] if tool_report else None,
                    "tool_discovered": tool_report.get("discovered") if tool_report else None,
                    "tool_invocation_count": tool_report.get("invocation_count", 0) if tool_report else 0,
                    "tool_invocations_omitted": tool_report.get("invocations_omitted", 0) if tool_report else 0,
                    "tool_metrics": tool_report.get("tool_metrics") if tool_report else None,
                    "tool_invocations": tool_report["invocations"] if tool_report else []}
                   if selected_tools is not None else {}),
                **(collected.model_dump(mode="json")["state"] if collected else {})},
            artifact_refs=collected.artifact_refs if collected else ())
