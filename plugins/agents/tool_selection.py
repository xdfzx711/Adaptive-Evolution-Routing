"""Codex tool artifact and per-session MCP configuration adapter."""

import json
from pathlib import Path
import sys
import tempfile

from plugins.tool_selection import ToolDescriptor, ToolSelection
from plugins.tools.generated.bundle import decode_tools, generated_tools, validate_tools


class CodexToolAdapter:
    compatible_agents = ("codex_cli_agent",)

    def catalog(self, snapshot, artifacts=None):
        if snapshot.agent_plugin_id not in self.compatible_agents:
            raise ValueError("Codex tool adapter requires a Codex Agent")
        catalog = {name: ToolDescriptor.model_validate(value)
                for name, value in snapshot.model_dump(mode="json")["config"].get("tool_catalog", {}).items()}
        if artifacts is not None:
            selection = decode_tools(artifacts.get(snapshot.artifacts["tool"]))
            catalog.update({name: tool.descriptor() for name, tool in generated_tools(selection).items()})
        return catalog

    def read(self, snapshot, artifacts):
        selection = decode_tools(artifacts.get(snapshot.artifacts["tool"]))
        configuration = snapshot.model_dump(mode="json")["config"]
        generated = configuration.get("tooling", {}).get("generated_tools")
        validate_tools(selection, self.catalog(snapshot),
            allow_generated=bool(configuration.get("generated_tools_enabled") or generated and generated.get("enabled")))
        return selection

    def encode(self, selection):
        return selection.model_dump_json(indent=2).encode("utf-8")

    def prepare(self, selection, config, workspace, project_root):
        return CodexToolSession(selection, config, workspace, project_root)


class CodexToolSession:
    def __init__(self, selection, config, workspace, project_root):
        self.selection = selection
        self.temporary = None
        self.report_path = None
        self.overrides = []
        if selection.enabled_tools:
            self.temporary = tempfile.TemporaryDirectory(prefix="aer-tool-session-")
            root = Path(self.temporary.name)
            values = config.model_dump(mode="json")
            if values["dotenv_path"]:
                path = Path(values["dotenv_path"])
                values["dotenv_path"] = str((project_root / path).resolve()) if not path.is_absolute() else str(path)
            config_path, self.report_path = root / "config.json", root / "status.json"
            config_path.write_text(json.dumps({"tooling": values, "selection": selection.model_dump(mode="json")},
                ensure_ascii=False, indent=2), encoding="utf-8")
            bridge = Path(__file__).resolve().parents[1] / "tools" / "mcp_bridge.py"
            envs = set()
            upstreams = set()
            for name in selection.enabled_tools:
                if name in generated_tools(selection):
                    continue
                entry = config.catalog[name]
                if entry.kind == "mcp":
                    upstreams.add(entry.server)
                    connection = config.servers[entry.server]
                    envs.update(connection.env_vars)
                    envs.update(connection.header_envs.values())
                    if connection.bearer_token_env:
                        envs.add(connection.bearer_token_env)
            # TOML string values are JSON-compatible; lists are rendered individually.
            fields = {"command": sys.executable,
                "args": [str(bridge), "--config", str(config_path), "--workspace", str(workspace), "--report", str(self.report_path)],
                "enabled": True, "required": True, "enabled_tools": list(selection.enabled_tools),
                "env_vars": sorted(envs), "startup_timeout_sec": config.startup_timeout_seconds * max(1, len(upstreams)) + 5,
                "tool_timeout_sec": config.tool_timeout_seconds + 5}
            self.overrides = ["mcp_servers.aer_tools={" + ",".join(
                key + "=" + json.dumps(value, ensure_ascii=False) for key, value in fields.items()) + "}"]

    def report(self):
        if not self.selection.enabled_tools:
            return {"status": "ready", "available_tools": [], "discovered": True,
                    "invocation_count": 0, "invocations": [], "invocations_omitted": 0, "tool_metrics": {}, "error": None}
        if self.report_path.is_file():
            try:
                return json.loads(self.report_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
        return {"status": "not_started", "available_tools": [], "discovered": False,
                "invocation_count": 0, "invocations": [], "error": "bridge_did_not_start"}

    def close(self):
        if self.temporary is not None:
            self.temporary.cleanup()
