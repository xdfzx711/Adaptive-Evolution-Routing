"""Execution-scoped MCP delivery of the shared skill runtime."""

import json
import os
from pathlib import Path
import sys
import tempfile

from .library import encode_library
from .runtime import READ_TOOL


def repository_skill_overrides(workspace):
    """Host-discovery opt-out does not disable repository skill discovery."""
    paths = set()
    for root in (workspace / ".agents/skills", workspace / ".codex/skills"):
        if root.is_dir():
            visited = set()
            for directory, children, files in os.walk(root, followlinks=True):
                resolved = Path(directory).resolve()
                if resolved in visited:
                    children[:] = []
                    continue
                visited.add(resolved)
                for name in files:
                    if name.lower() == "skill.md":
                        entry = Path(directory) / name
                        paths.update((str(entry), str(entry.resolve())))
    return "skills.config=[" + ",".join(
        "{path=" + json.dumps(path, ensure_ascii=False) + ",enabled=false}" for path in sorted(paths)) + "]"


class CodexSkillSession:
    def __init__(self, runtime):
        self.runtime = runtime
        self.temporary = tempfile.TemporaryDirectory(prefix="aer-skill-session-")
        try:
            root = Path(self.temporary.name)
            self.report_path = root / "status.json"
            config_path = root / "library.json"
            config_path.write_text(json.dumps({"library": json.loads(encode_library(runtime.library)),
                "artifact_id": runtime.artifact_id, "snapshot_version_id": runtime.snapshot_version_id,
                "execution_operation_id": runtime.execution_operation_id}, ensure_ascii=False), encoding="utf-8")
            fields = {"command": sys.executable,
                "args": [str(Path(__file__).with_name("mcp_bridge.py")), "--library", str(config_path),
                         "--report", str(self.report_path)],
                "enabled": True, "required": True, "enabled_tools": [READ_TOOL],
                "startup_timeout_sec": 30, "tool_timeout_sec": 30}
            self.overrides = ["mcp_servers.aer_skills={" + ",".join(
                key + "=" + json.dumps(value, ensure_ascii=False) for key, value in fields.items()) + "}"]
        except Exception:
            self.temporary.cleanup()
            raise

    def report(self):
        if not self.report_path.is_file():
            return {"status": "not_started", "discovered": False, "reads": []}
        report = json.loads(self.report_path.read_text(encoding="utf-8"))
        if (not isinstance(report, dict) or report.get("status") != "ready"
                or type(report.get("discovered")) is not bool or not isinstance(report.get("reads"), list)):
            raise ValueError("invalid skill service report")
        for item in report["reads"]:
            required = {"snapshot_version_id", "skill_library_artifact_id", "execution_operation_id",
                        "name", "call_id", "delivery", "status"}
            if (not isinstance(item, dict) or not required <= item.keys()
                    or any(not isinstance(item[key], str) for key in required)
                    or item["delivery"] != "read" or item["status"] not in {"completed", "error"}
                    or (item["status"] == "completed" and not isinstance(item.get("content_digest"), str))):
                raise ValueError("invalid skill read receipt")
            if (item["snapshot_version_id"], item["skill_library_artifact_id"], item["execution_operation_id"]) != (
                    self.runtime.snapshot_version_id, self.runtime.artifact_id, self.runtime.execution_operation_id):
                raise ValueError("skill receipt belongs to a different execution")
        self.runtime.reads = report["reads"]
        return report

    def close(self):
        self.temporary.cleanup()
