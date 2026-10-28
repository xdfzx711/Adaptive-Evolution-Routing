"""Public task-runtime facts and the installed CLI tools' read-only roots."""

import os
from pathlib import Path
import shutil


def native_tools(project_root):
    executables, roots = {}, set()
    for name in ("git", "rg"):
        executable = shutil.which(name + ".exe" if os.name == "nt" else name)
        if executable is None:
            continue
        path = Path(executable).resolve()
        root = path.parent
        if os.name == "nt" and name == "git" and root.name.lower() in {"cmd", "bin"}:
            if (root.parent / "mingw64").is_dir():
                root = root.parent  # Git's launcher needs its adjacent DLLs.
        if root.is_relative_to(project_root) or project_root.is_relative_to(root):
            continue
        executables[name] = str(path)
        if os.name == "nt":
            roots.add(root)
    return executables, tuple(sorted(roots))


def task_environment(workspace, tools):
    result = {"platform": os.name, "workspace": str(workspace), "tool_executables": tools}
    if os.name == "nt":
        result["instructions"] = (
            "This is a native Windows workspace using PowerShell, not a POSIX shell. "
            "Use Get-Content/Get-ChildItem -LiteralPath to read files. "
            "Invoke supplied absolute executable paths with PowerShell's call operator, for example "
            "& '<git executable>' -C '<workspace>' status --short and & '<rg executable>' -n '<pattern>' '<path>'. "
            "Git already trusts this task workspace; do not change global Git settings. "
            "Quote regex patterns and avoid nesting PowerShell, cmd.exe and shell strings. "
            "Do not try bash/WSL or inspect host directories to locate tools. "
            "Read only the relevant source ranges and keep tool output concise.")
    return result


def error_message(value):
    if isinstance(value, dict):
        return str(value.get("message", value))
    return str(value)
